"""Project-local query-by-example matching and selective Foley replacement."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
import uuid
import wave
from pathlib import Path

import numpy as np
from numpy.lib.format import open_memmap

from ..config import SAMPLE_RATE as RATE
from ..ops import observability as obs
from . import media, projects
from .projects import atomic, ff, load, project_dir

INDEX_SCHEMA = "sound-index.v1"
FAMILY_SCHEMA = "sound-family.v1"
MATCH_SCHEMA = "sound-match.v1"
INDEX_RATE = 12_000
WINDOW_S = 0.64
HOP_S = 0.10
PRE_ONSET_S = 0.08
BANDS = 24
TIME_BINS = 8
FEATURES = BANDS * TIME_BINS
MAX_MATCHES = 100
MAX_FAMILIES = 50
MAX_EXAMPLES = 8
MIN_SEED_S = 0.04
MAX_SEED_S = 4.0
MIN_MATCH_GAP_S = 0.30
MIN_SIMILARITY_SCORE = 0.45
MAX_EXAMPLE_ENERGY_DROP_DB = 8
BATCH_WINDOWS = 128
MIX_CHUNK_FRAMES = RATE * 10

INDEX_CONFIG = {
    "rate": INDEX_RATE,
    "window_s": WINDOW_S,
    "hop_s": HOP_S,
    "pre_onset_s": PRE_ONSET_S,
    "bands": BANDS,
    "time_bins": TIME_BINS,
    "feature": "normalized_log_power_time_frequency",
}

def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)

def _wav_shape(path):
    with wave.open(str(path), "rb") as stream:
        rate, channels, width, frames = (
            stream.getframerate(),
            stream.getnchannels(),
            stream.getsampwidth(),
            stream.getnframes(),
        )
    if rate != RATE or channels not in (1, 2) or width != 2:
        raise ValueError("Expected 48kHz mono or stereo PCM16 WAV")
    return frames, channels

def _read_range(path, start_frame, frame_count):
    frames, channels = _wav_shape(path)
    start_frame = max(0, min(int(start_frame), frames))
    frame_count = max(0, min(int(frame_count), frames - start_frame))
    with wave.open(str(path), "rb") as stream:
        stream.setpos(start_frame)
        raw = stream.readframes(frame_count)
    values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    if channels == 2:
        values = values.reshape(-1, 2)
    return values

def _mono(samples):
    return samples.mean(axis=1) if samples.ndim == 2 else samples

def _band_matrix(fft_samples):
    frequencies = np.fft.rfftfreq(fft_samples, 1 / INDEX_RATE)
    edges = np.geomspace(45, INDEX_RATE / 2, BANDS + 1)
    matrix = np.zeros((len(frequencies), BANDS), dtype=np.float32)
    for band, (low, high) in enumerate(zip(edges[:-1], edges[1:])):
        selected = (frequencies >= low) & (frequencies < high)
        if np.any(selected):
            matrix[selected, band] = 1 / np.sum(selected)
    return matrix

def _fingerprints(windows):
    """Return deterministic amplitude-invariant fingerprints for PCM windows."""
    windows = np.asarray(windows, dtype=np.float32)
    if windows.ndim == 1:
        windows = windows[None, :]
    expected = round(WINDOW_S * RATE)
    if windows.shape[1] != expected:
        raise ValueError("Fingerprint windows have the wrong duration")
    reduced = windows.reshape(len(windows), -1, RATE // INDEX_RATE).mean(axis=2)
    frame_samples = reduced.shape[1] // TIME_BINS
    frames = reduced[:, : frame_samples * TIME_BINS].reshape(
        len(windows), TIME_BINS, frame_samples
    )
    frames *= np.hanning(frame_samples).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, n=1024, axis=2)) ** 2
    bands = np.einsum("ntf,fb->ntb", power, _band_matrix(1024), optimize=True)
    features = np.log1p(bands * 1e6).reshape(len(windows), FEATURES).astype(np.float32)
    features -= np.mean(features, axis=1, keepdims=True)
    norms = np.linalg.norm(features, axis=1, keepdims=True)
    return features / np.maximum(norms, 1e-8)

def _window_count(frame_count):
    window = round(WINDOW_S * RATE)
    hop = round(HOP_S * RATE)
    return max(1, math.ceil(max(0, frame_count - window) / hop) + 1)

def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def _index_folder(pid):
    folder = project_dir(pid) / "similarity"
    folder.mkdir(exist_ok=True)
    return folder

def _index_paths(pid):
    folder = _index_folder(pid)
    return folder / "index.json", folder / "features.npy", folder / "energy.npy"

def index_status(pid):
    metadata, _, _ = _index_paths(pid)
    if not metadata.exists():
        return {"schema": INDEX_SCHEMA, "status": "absent", "completed_windows": 0}
    state = json.loads(metadata.read_text())
    return {**state, "total_windows": state.get("window_count")}

def build_index(pid):
    """Build or resume the bounded on-disk fingerprint index for one project."""
    case = load(pid)
    audio = Path(case["original_path"])
    frame_count, _ = _wav_shape(audio)
    count = _window_count(frame_count)
    source_hash = _sha256(audio)
    cache_key = hashlib.sha256(
        json.dumps(
            {"audio_sha256": source_hash, "config": INDEX_CONFIG}, sort_keys=True
        ).encode()
    ).hexdigest()
    metadata_path, features_path, energy_path = _index_paths(pid)
    prior = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    matching = (
        prior.get("schema") == INDEX_SCHEMA
        and prior.get("cache_key") == cache_key
        and prior.get("window_count") == count
    )
    if matching and prior.get("status") == "ready":
        try:
            if (
                np.load(features_path, mmap_mode="r").shape == (count, FEATURES)
                and np.load(energy_path, mmap_mode="r").shape == (count,)
            ):
                return prior
        except (OSError, ValueError):
            pass
    completed = min(int(prior.get("completed_windows", 0)), count) if matching else 0
    try:
        if completed:
            vectors = open_memmap(features_path, mode="r+")
            energies = open_memmap(energy_path, mode="r+")
            if vectors.shape != (count, FEATURES) or energies.shape != (count,):
                completed = 0
        if not completed:
            vectors = open_memmap(
                features_path, mode="w+", dtype=np.float32, shape=(count, FEATURES)
            )
            energies = open_memmap(
                energy_path, mode="w+", dtype=np.float32, shape=(count,)
            )
        state = {
            "schema": INDEX_SCHEMA,
            "status": "building",
            "cache_key": cache_key,
            "audio_sha256": source_hash,
            "duration_s": frame_count / RATE,
            "window_count": count,
            "completed_windows": completed,
            "feature_count": FEATURES,
            "config": INDEX_CONFIG,
        }
        atomic(metadata_path, state)
        window = round(WINDOW_S * RATE)
        hop = round(HOP_S * RATE)
        for first in range(completed, count, BATCH_WINDOWS):
            last = min(count, first + BATCH_WINDOWS)
            begin = first * hop
            span = (last - first - 1) * hop + window
            audio_batch = _mono(_read_range(audio, begin, span))
            if len(audio_batch) < span:
                audio_batch = np.pad(audio_batch, (0, span - len(audio_batch)))
            windows = np.lib.stride_tricks.sliding_window_view(audio_batch, window)[
                ::hop
            ][: last - first]
            vectors[first:last] = _fingerprints(windows)
            rms = np.sqrt(np.mean(windows * windows, axis=1))
            energies[first:last] = 20 * np.log10(np.maximum(rms, 1e-9))
            vectors.flush()
            energies.flush()
            state["completed_windows"] = last
            atomic(metadata_path, state)
        state.update(status="ready", completed_windows=count, indexed_at=time.time())
        atomic(metadata_path, state)
        return state
    except Exception:
        if "state" in locals():
            state["status"] = "interrupted"
            atomic(metadata_path, state)
        raise

def _family_folder(pid):
    folder = project_dir(pid) / "families"
    folder.mkdir(exist_ok=True)
    return folder

def _family_path(pid, family_id):
    if not isinstance(family_id, str) or not __import__("re").fullmatch(
        "[a-f0-9]{12}", family_id
    ):
        raise ValueError("Invalid sound family ID")
    return _family_folder(pid) / f"{family_id}.json"

def list_families(pid):
    return [
        json.loads(path.read_text()) for path in sorted(_family_folder(pid).glob("*.json"))
    ]

def _load_family(pid, family_id):
    doc = json.loads(_family_path(pid, family_id).read_text())
    if doc.get("schema") != FAMILY_SCHEMA or doc.get("project_id") != pid:
        raise ValueError("Invalid sound family")
    return doc

get = _load_family
def _range(value, duration, label="range"):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be [start, end]")
    start, end = (_finite(x, label) for x in value)
    if not 0 <= start < end <= duration:
        raise ValueError(f"{label} outside media")
    return [start, end]

def _refine(audio, bounds):
    start, end = bounds
    samples = _mono(
        _read_range(audio, round(start * RATE), max(1, round((end - start) * RATE)))
    )
    if not len(samples):
        return (start + end) / 2
    return start + int(np.argmax(np.abs(samples))) / RATE

def _feature_at(audio, bounds):
    anchor = _refine(audio, bounds)
    frame = round((anchor - PRE_ONSET_S) * RATE)
    before = max(0, -frame)
    samples = _mono(_read_range(audio, max(0, frame), round(WINDOW_S * RATE)))
    if before:
        samples = np.pad(samples, (before, 0))
    samples = samples[: round(WINDOW_S * RATE)]
    if len(samples) < round(WINDOW_S * RATE):
        samples = np.pad(samples, (0, round(WINDOW_S * RATE) - len(samples)))
    return _fingerprints(samples)[0], anchor

def _match_id(cache_key, index):
    return hashlib.sha256(f"{cache_key}:{index}".encode()).hexdigest()[:12]

def _rank(case, index, accepted, rejected, excluded):
    vectors = np.load(_index_paths(case["id"])[1], mmap_mode="r")
    energies = np.load(_index_paths(case["id"])[2], mmap_mode="r")
    positive_rows = [_feature_at(case["original_path"], item["range_s"]) for item in accepted]
    positives = [row[0] for row in positive_rows]
    negatives = [_feature_at(case["original_path"], item["range_s"])[0] for item in rejected]
    # Keep distinct confirmed sounds distinct: a heel strike and a soft grass step
    # should each retrieve their nearest acoustic neighbours, not blur into an average.
    positive_scores = np.asarray(vectors @ np.stack(positives).T)
    nearest_examples = positive_scores.argmax(axis=1)
    scores = positive_scores.max(axis=1)
    example_energy = np.asarray([energies[min(len(energies) - 1, max(0, round((row[1] - PRE_ONSET_S) / HOP_S)))] for row in positive_rows])
    scores = np.where(np.asarray(energies) >= example_energy[nearest_examples] - MAX_EXAMPLE_ENERGY_DROP_DB, scores, -np.inf)
    if negatives:
        negative = np.asarray(vectors @ np.stack(negatives).T).max(axis=1)
        scores -= 0.35 * np.maximum(negative, 0)
    scores = np.where(np.asarray(energies) > -65, scores, -np.inf)
    # Refine a bounded oversupply before collapsing adjacent windows around one event.
    supply = min(len(scores), max(MAX_MATCHES * 8, MAX_MATCHES))
    indices = np.argpartition(scores, -supply)[-supply:]
    indices = indices[np.argsort(scores[indices])[::-1]]
    results = []
    chosen = [item["refined_anchor_s"] for item in accepted + rejected]
    duration = float(case["seconds"])
    for index_number in indices:
        index_number = int(index_number)
        match_id = _match_id(index["cache_key"], index_number)
        if match_id in excluded or not math.isfinite(float(scores[index_number])):
            continue
        if float(scores[index_number]) < MIN_SIMILARITY_SCORE:
            break
        start = index_number * HOP_S
        end = min(duration, start + WINDOW_S)
        anchor = _refine(case["original_path"], [start, end])
        if any(abs(anchor - earlier) < MIN_MATCH_GAP_S for earlier in chosen):
            continue
        bounds = [max(0, anchor - PRE_ONSET_S), min(duration, anchor + WINDOW_S - PRE_ONSET_S)]
        if any(max(bounds[0], item["range_s"][0]) < min(bounds[1], item["range_s"][1]) for item in accepted):
            continue
        chosen.append(anchor)
        score = float(scores[index_number])
        example = accepted[int(nearest_examples[index_number])]
        results.append(
            {
                "schema": MATCH_SCHEMA,
                "id": match_id,
                "range_s": [round(x, 6) for x in bounds],
                "refined_anchor_s": round(anchor, 6),
                "similarity_score": round(score, 6),
                "matched_example_id": example["id"],
                "decision": "pending",
                "index_window": index_number,
                "evidence_summary": (
                    f"Nearest confirmed example {example['id']} scored {score:.3f}; onset "
                    "refined to the strongest local sample. Ranking evidence only."
                ),
            }
        )
        if len(results) == MAX_MATCHES:
            break
    return results

def add_example(pid, family_id, range_s):
    from .family_actions import add_example as action
    return action(pid, family_id, range_s)

def create(pid, name, seed_range_s, *, defer=False):
    case = load(pid)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Sound family name must be 1..80 characters")
    if len(list_families(pid)) >= MAX_FAMILIES:
        raise ValueError(f"A project supports at most {MAX_FAMILIES} sound families")
    seed_range = _range(seed_range_s, float(case["seconds"]), "seed range")
    if not MIN_SEED_S <= seed_range[1] - seed_range[0] <= MAX_SEED_S:
        raise ValueError(f"Seed duration must be {MIN_SEED_S}..{MAX_SEED_S} seconds")
    _, anchor = _feature_at(case["original_path"], seed_range)
    seed = {
        "schema": MATCH_SCHEMA,
        "id": "seed",
        "range_s": seed_range,
        "refined_anchor_s": round(anchor, 6),
        "similarity_score": 1.0,
        "decision": "accepted",
        "kind": "example",
        "evidence_summary": "User-confirmed query example.",
    }
    family_id = uuid.uuid4().hex[:12]
    doc = {
        "schema": FAMILY_SCHEMA,
        "id": family_id,
        "project_id": pid,
        "name": name.strip(),
        "seed_range_s": seed_range,
        "accepted_ranges": [seed],
        "rejected_ranges": [],
        "pending_matches": [],
        "replacement_take_id": None,
        "search_version": 1,
        "status": "part_ready" if defer else "indexing",
        "scope": "part" if defer else "movie",
        "created_at": time.time(),
        "index_cache_key": None,
        "warning": "Similarity scores rank acoustic resemblance; they are not probabilities or semantic labels.",
        "warnings": [
            "Similarity ranks acoustic resemblance; every proposed match requires review."
        ],
    }
    atomic(_family_path(pid, family_id), doc)
    obs.emit(pid, "family_created", {
        "name": doc["name"], "status": doc["status"], "start_s": seed_range[0], "end_s": seed_range[1]
    })
    return doc if defer else search(pid, family_id)

def search(pid, family_id):
    """Build/resume the index and refresh one family's pending review queue."""
    doc = _load_family(pid, family_id)
    if doc.get("scope") == "part":
        if not doc.get("replacement_take_id"):
            raise ValueError("Assign a replacement SFX before searching the full movie")
        approved = doc.get("approved_agent_fitting", {})
        if approved.get("take_id") != doc.get("replacement_take_id"):
            raise ValueError("Approve the fitted part before searching the full movie")
    doc["status"] = "searching"
    atomic(_family_path(pid, family_id), doc)
    try:
        index = build_index(pid)
        excluded = {
            item["id"] for item in doc["accepted_ranges"] + doc["rejected_ranges"]
        }
        doc["pending_matches"] = _rank(
            load(pid), index, doc["accepted_ranges"], doc["rejected_ranges"], excluded
        )
        doc["index_cache_key"] = index["cache_key"]
        doc["scope"] = "movie"
        doc["propagated_at"] = time.time()
        doc["status"] = "review_required" if doc["pending_matches"] else "ready"
        doc["updated_at"] = time.time()
        atomic(_family_path(pid, family_id), doc)
        obs.emit(pid, "family_movie_search", {
            "family_id": family_id,
            "status": doc["status"],
            "start_s": doc["seed_range_s"][0],
            "end_s": doc["seed_range_s"][1],
            "measurements": {"pending_matches": len(doc["pending_matches"])},
        })
        for item in doc["pending_matches"]:
            obs.emit(pid, "family_range", {
                "family_id": family_id, "mapping_id": item["id"], "status": "pending",
                "start_s": item["range_s"][0], "end_s": item["range_s"][1],
            })
        return doc
    except Exception:
        doc["status"] = "indexing_failed"
        doc["updated_at"] = time.time()
        atomic(_family_path(pid, family_id), doc)
        raise

def review(pid, family_id, accepted_ids, rejected_ids):
    doc = _load_family(pid, family_id)
    if not isinstance(accepted_ids, list) or not isinstance(rejected_ids, list):
        raise ValueError("Accepted and rejected IDs must be lists")
    accepted_ids, rejected_ids = set(accepted_ids), set(rejected_ids)
    if accepted_ids & rejected_ids or any(
        not isinstance(item, str) for item in accepted_ids | rejected_ids
    ):
        raise ValueError("Match decisions must be disjoint string IDs")
    pending = {item["id"]: item for item in doc["pending_matches"]}
    decided = accepted_ids | rejected_ids
    if not decided <= set(pending):
        raise ValueError("Only pending matches can be reviewed")
    doc["accepted_ranges"] += [
        {**item, "decision": "accepted"}
        for item in doc["pending_matches"]
        if item["id"] in accepted_ids
    ]
    doc["rejected_ranges"] += [
        {**item, "decision": "rejected"}
        for item in doc["pending_matches"]
        if item["id"] in rejected_ids
    ]
    index = build_index(pid)
    excluded = {
        item["id"] for item in doc["accepted_ranges"] + doc["rejected_ranges"]
    }
    doc["pending_matches"] = _rank(
        load(pid), index, doc["accepted_ranges"], doc["rejected_ranges"], excluded
    )
    if doc.get("latest_render_id"):
        doc["render_progress"] = {
            "status": "stale",
            "kind": "full_movie",
            "phase": "stale",
            "progress": 0,
            "message": "Reviewed matches changed. Build a new full-movie preview.",
            "updated_at": time.time(),
        }
    doc["search_version"] += 1
    doc["status"] = "review_required" if doc["pending_matches"] else "ready"
    doc["updated_at"] = time.time()
    atomic(_family_path(pid, family_id), doc)
    obs.emit(pid, "family_review", {"status": doc["status"], "measurements": {
        "accepted": len(accepted_ids), "rejected": len(rejected_ids)
    }})
    for status, items in (("accepted", doc["accepted_ranges"]), ("rejected", doc["rejected_ranges"])):
        for item in items:
            obs.emit(pid, "family_range", {
                "family_id": family_id, "mapping_id": item["id"], "status": status,
                "start_s": item["range_s"][0], "end_s": item["range_s"][1],
            })
    return doc

def assign_take(pid, family_id, take_id):
    from .family_actions import assign_take as action
    return action(pid, family_id, take_id)

def _possible_dialogue_or_music(samples):
    mono = _mono(samples)
    if len(mono) < RATE // 20:
        return False
    rms = float(np.sqrt(np.mean(mono * mono)))
    if rms < 10 ** (-45 / 20):
        return False
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
    frequencies = np.fft.rfftfreq(len(mono), 1 / RATE)
    total = float(np.sum(spectrum[(frequencies >= 60) & (frequencies <= 12_000)]))
    voice = float(np.sum(spectrum[(frequencies >= 150) & (frequencies <= 4_000)]))
    crest = media.db(np.max(np.abs(mono))) - media.db(rms)
    return total > 0 and voice / total > 0.65 and crest < 18

def mix_selective(original, replacement, matches, *, duck_db=-12, ramp_s=0.025, replacement_gain_db=0):
    """Duck only accepted windows and overlay one peak-aligned replacement."""
    original = np.asarray(original, dtype=np.float32)
    replacement = _mono(np.asarray(replacement, dtype=np.float32))
    if original.ndim not in (1, 2) or original.shape[0] < 1 or replacement.ndim != 1:
        raise ValueError("Audio must be nonempty mono/stereo PCM")
    duck_db = _finite(duck_db, "duck_db")
    ramp_s = _finite(ramp_s, "ramp_s")
    replacement_gain_db = _finite(replacement_gain_db, "replacement_gain_db")
    if not -30 <= duck_db <= 0 or not 0.005 <= ramp_s <= 0.25 or not -30 <= replacement_gain_db <= 12:
        raise ValueError("Mix controls outside bounds")
    if not matches:
        raise ValueError("At least one accepted match is required")
    source_peak = int(np.argmax(np.abs(replacement)))
    crop_start = max(0, source_peak - round(PRE_ONSET_S * RATE))
    crop_end = min(len(replacement), crop_start + round(WINDOW_S * RATE))
    part = replacement[crop_start:crop_end].copy() * 10 ** (replacement_gain_db / 20)
    source_anchor = source_peak - crop_start
    envelope = np.ones(original.shape[0], dtype=np.float32)
    layer = np.zeros_like(original, dtype=np.float32)
    ranges = []
    dialogue = []
    for item in matches:
        start_s, end_s = _range(item["range_s"], original.shape[0] / RATE, "match range")
        anchor_s = _finite(item["refined_anchor_s"], "refined anchor")
        if not start_s <= anchor_s <= end_s:
            raise ValueError("Refined anchor outside match")
        start, end = round(start_s * RATE), round(end_s * RATE)
        ramp = min(round(ramp_s * RATE), (end - start) // 2)
        floor = 10 ** (duck_db / 20)
        curve = np.full(end - start, floor, dtype=np.float32)
        if ramp:
            curve[:ramp] = np.linspace(1, floor, ramp, endpoint=False)
            curve[-ramp:] = np.linspace(floor, 1, ramp, endpoint=False)
        envelope[start:end] = np.minimum(envelope[start:end], curve)
        destination = round(anchor_s * RATE) - source_anchor
        source_skip = max(0, -destination)
        destination = max(0, destination)
        count = min(len(part) - source_skip, end - destination, len(original) - destination)
        if count < 1:
            raise ValueError("Replacement falls outside accepted match")
        placed = part[source_skip : source_skip + count].copy()
        fade = min(ramp, len(placed) // 2)
        if fade:
            placed[:fade] *= np.linspace(0, 1, fade, endpoint=False)
            placed[-fade:] *= np.linspace(1, 0, fade, endpoint=False)
        if original.ndim == 2:
            layer[destination : destination + count] += placed[:, None]
        else:
            layer[destination : destination + count] += placed
        ranges.append([start_s, end_s])
        if _possible_dialogue_or_music(original[start:end]):
            dialogue.append(item["id"])
    ducked = original * (envelope[:, None] if original.ndim == 2 else envelope)
    active = np.abs(layer) > 1e-9
    upper = np.ones_like(layer, dtype=np.float32)
    upper[active & (layer > 0)] = (0.999 - ducked[active & (layer > 0)]) / layer[active & (layer > 0)]
    upper[active & (layer < 0)] = (-0.999 - ducked[active & (layer < 0)]) / layer[active & (layer < 0)]
    scale = min(1.0, max(0.0, float(np.min(upper[active])))) if np.any(active) else 1.0
    mixed = ducked + layer * scale
    if float(np.max(np.abs(mixed))) >= 1:
        raise ValueError("Replacement mix clips; lower the replacement gain")
    return mixed, {
        "duck_db": duck_db,
        "ramp_s": ramp_s,
        "replacement_gain_db": replacement_gain_db,
        "replacement_peak_protection_db": round(20 * math.log10(max(scale, 1e-9)), 3),
        "ranges_s": ranges,
        "possible_dialogue_or_music_overlap_ids": dialogue,
        "warning": "Dialogue/music overlap is a conservative spectral heuristic, not source separation.",
    }

def _read_pcm(path):
    frames, channels = _wav_shape(path)
    samples = _read_range(path, 0, frames)
    return samples.reshape(-1, channels) if channels == 2 else samples

def _write_pcm(path, samples):
    samples = np.asarray(samples, dtype=np.float32)
    channels = 2 if samples.ndim == 2 else 1

    def _emit(target):
        with wave.open(str(target), "wb") as output:
            output.setparams((channels, 2, RATE, len(samples), "NONE", "not compressed"))
            output.writeframes(
                np.clip(np.round(samples * 32768), -32768, 32767).astype("<i2").tobytes()
            )

    # wave.open rewrites the RIFF header on close, which gcsfuse refuses.
    if not projects._needs_staging(path):
        _emit(path)
        return
    with tempfile.TemporaryDirectory(prefix="orpheus-pcm-") as staging:
        local = Path(staging) / Path(str(path)).name
        _emit(local)
        Path(str(path)).parent.mkdir(parents=True, exist_ok=True)
        projects.publish(local, path)

def _pcm_chunks(path):
    frames, channels = _wav_shape(path)
    with wave.open(str(path), "rb") as stream:
        offset = 0
        while offset < frames:
            raw = stream.readframes(min(MIX_CHUNK_FRAMES, frames - offset))
            values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
            if channels == 2:
                values = values.reshape(-1, 2)
            yield offset, values
            offset += len(values)

def _write_selective_wav(
    original_path,
    output_path,
    replacement,
    matches,
    duck_db,
    ramp_s,
    replacement_gain_db,
    *,
    variants=None,
    timeline_offset_s=0,
    on_progress=None,
):
    from .family_render import write_selective_wav

    return write_selective_wav(
        original_path, output_path, replacement, matches, duck_db, ramp_s,
        replacement_gain_db, variants=variants, timeline_offset_s=timeline_offset_s,
        on_progress=on_progress,
    )

def render(pid, family_id, take_id=None, folder=None, *, duck_db=-12,
           ramp_s=0.025, replacement_gain_db=0, case=None, accepted=None,
           persist=True):
    full_movie = case is None
    case = case or load(pid)
    family = _load_family(pid, family_id)
    take_id = take_id or family.get("replacement_take_id")
    if family.get("replacement_take_id") != take_id:
        raise ValueError("Assign the take to this family before rendering")
    accepted = accepted or family["accepted_ranges"]
    if len(accepted) < 1:
        raise ValueError("Review at least one match before rendering")
    mix_path = Path(case.get("mix_path", case["original_path"]))
    from . import takes

    source_path, _ = takes.validated_audio(pid, family_id, take_id)
    duck_db = _finite(duck_db, "duck_db")
    ramp_s = _finite(ramp_s, "ramp_s")
    replacement_gain_db = _finite(replacement_gain_db, "replacement_gain_db")
    if not -30 <= duck_db <= 0 or not 0.005 <= ramp_s <= 0.25 or not -30 <= replacement_gain_db <= 12:
        raise ValueError("Mix controls outside bounds")
    folder = Path(folder or project_dir(pid))
    folder.mkdir(parents=True, exist_ok=True)
    render_id = uuid.uuid4().hex[:12]
    wav_path, video_path = folder / f"{render_id}.wav", folder / f"{render_id}.mp4"
    last_progress = {"phase": "", "value": -1}

    def report(phase, value, message):
        value = max(0, min(100, int(round(value))))
        if phase == last_progress["phase"] and value <= last_progress["value"]:
            return
        last_progress.update(phase=phase, value=value)
        obs.emit(pid, "render_progress", {
            "family_id": family_id,
            "candidate_id": render_id,
            "name": phase,
            "status": "running",
            "kind": "full_movie" if full_movie else "part",
            "measurements": {"progress": value},
        })
        if full_movie and persist:
            family["render_progress"] = {
                "status": "running",
                "kind": "full_movie",
                "render_id": render_id,
                "phase": phase,
                "progress": value,
                "message": message,
                "updated_at": time.time(),
            }
            atomic(_family_path(pid, family_id), family)

    learned = family.get("approved_agent_fitting", {})
    variants = learned.get("agent_fitting", {}).get("arrangement", {}).get("rows", [])
    report("preparing", 0, "Preparing the full-movie selective render")
    try:
        mix = _write_selective_wav(
            mix_path, wav_path, _mono(_read_pcm(source_path)), accepted,
            duck_db, ramp_s, replacement_gain_db, variants=variants,
            timeline_offset_s=float(learned.get("timeline_offset_s", 0)), on_progress=report,
        )
        report("preview", 84, "Muxing the browser preview")
        ff(
            "-i", case["video_path"], "-i", wav_path,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-t", case["seconds"], video_path,
        )
        source_video = Path(case.get("source_video_path", case["video_path"]))
        master_suffix = ".mp4" if source_video.suffix.lower() == ".mp4" else ".mkv"
        master_path = folder / f"{render_id}-master{master_suffix}"
        report("master", 90, "Muxing the preserved-picture master")
        ff(
            "-i", source_video, "-i", wav_path, "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", case["seconds"], master_path,
        )
        report("picture", 95, "Checking picture integrity")
        if media.picture_hash(source_video) != media.picture_hash(master_path):
            raise ValueError("Picture preservation failed")
        report("measure", 98, "Measuring loudness and clipping")
        metrics = {**media.measure_export(video_path), "picture_unchanged": True, "clipped_samples": 0}
    except Exception:
        if full_movie and persist:
            family["render_progress"] = {
                "status": "failed", "kind": "full_movie", "render_id": render_id,
                "phase": "failed", "progress": last_progress["value"],
                "message": "Full-movie preview failed before completion.", "updated_at": time.time(),
            }
            atomic(_family_path(pid, family_id), family)
        raise
    receipt = {
        "id": render_id,
        "schema": "family-render.v1",
        "project_id": pid,
        "family_id": family_id,
        "take_id": take_id,
        "video": video_path.name,
        "master": master_path.name,
        "wav": wav_path.name,
        "render_mode": "selective_duck_overlay",
        "timeline_offset_s": case.get("timeline_offset_s", 0),
        "preview_duration_s": case["seconds"],
        "audio_sha256": _sha256(wav_path),
        "arrangement": {"schema": "family-arrangement.v1", "rows": [
                {
                    "id": item["id"], "family_id": family_id, "take_id": take_id,
                    "target_range_s": item["range_s"], "target_anchor_s": item["refined_anchor_s"],
                }
                for item in accepted
            ]},
        "mix": mix,
        "metrics": metrics,
        "human_approved": False,
        "warning": "Only accepted windows were ducked. No source separation was applied.",
    }
    covered = [bound for item in accepted for bound in item["range_s"]]
    if len(set(covered)) > 1:
        receipt.update(obs.part_context(pid, family_id, min(covered), max(covered)))
    atomic(folder / f"{render_id}.json", receipt)
    if persist:
        family["latest_render_id"] = render_id
        family["latest_render"] = {key: receipt[key] for key in (
            "id", "video", "master", "wav", "render_mode", "audio_sha256",
            "timeline_offset_s", "preview_duration_s", "arrangement", "mix",
            "metrics", "human_approved", "warning")}
        if full_movie:
            family["render_progress"] = {
                "status": "complete", "kind": "full_movie", "render_id": render_id,
                "phase": "ready", "progress": 100,
                "message": "Full-movie preview ready for review.", "updated_at": time.time(),
            }
        family["updated_at"] = time.time()
        atomic(_family_path(pid, family_id), family)
    obs.emit(pid, "candidate", {
        **receipt, "measurements": {"accepted_events": len(accepted)}})
    return receipt


def preview_match(pid, family_id, match_id):
    """Build a bounded audition for one reviewed or pending movie match."""
    family = _load_family(pid, family_id)
    matches = {
        item["id"]: item
        for item in [*family.get("accepted_ranges", []), *family.get("pending_matches", [])]
    }
    if not isinstance(match_id, str) or match_id not in matches:
        raise ValueError("Choose a current family match to audition")
    take_id = family.get("replacement_take_id")
    if not take_id:
        raise ValueError("Assign a replacement take before auditioning a match")
    from . import takes

    case = load(pid)
    source_path, _ = takes.validated_audio(pid, family_id, take_id)
    item = matches[match_id]
    start_s, end_s = _range(item["range_s"], float(case["seconds"]), "match range")
    duration = min(15.0, float(case["seconds"]))
    center = _finite(item["refined_anchor_s"], "refined anchor")
    offset = max(0.0, min(float(case["seconds"]) - duration, center - duration / 2))
    signature = json.dumps([family_id, match_id, take_id, _sha256(source_path), start_s, end_s, offset])
    preview_id = hashlib.sha256(signature.encode()).hexdigest()[:12]
    folder = project_dir(pid)
    receipt_path = folder / f"{preview_id}.json"
    wav_path = folder / f"{preview_id}.wav"
    if receipt_path.exists() and wav_path.exists():
        cached = json.loads(receipt_path.read_text())
        if cached.get("preview_kind") == "match_audition" and cached.get("audio_sha256") == _sha256(wav_path):
            return cached
    frames = round(duration * RATE)
    scratch = folder / f".{preview_id}-original.wav"
    _write_pcm(scratch, _read_range(Path(case.get("mix_path", case["original_path"])), round(offset * RATE), frames))
    local = {
        **item,
        "kind": "match",
        "range_s": [start_s - offset, end_s - offset],
        "refined_anchor_s": center - offset,
    }
    learned = family.get("approved_agent_fitting", {})
    variants = learned.get("agent_fitting", {}).get("arrangement", {}).get("rows", [])
    try:
        mix = _write_selective_wav(
            scratch, wav_path, _mono(_read_pcm(source_path)), [local], -12, 0.025, 0,
            variants=variants,
        )
    finally:
        scratch.unlink(missing_ok=True)
    receipt = {
        "id": preview_id,
        "schema": "family-render.v1",
        "project_id": pid,
        "family_id": family_id,
        "take_id": take_id,
        "video": "",
        "master": "",
        "wav": wav_path.name,
        "render_mode": "selective_duck_overlay",
        "timeline_offset_s": offset,
        "preview_duration_s": duration,
        "audio_sha256": _sha256(wav_path),
        "preview_kind": "match_audition",
        "source_match_id": match_id,
        "mix": mix,
        "metrics": {},
        "human_approved": False,
        "warning": "Audition only. This does not alter the full-movie candidate or its approval state.",
    }
    atomic(receipt_path, receipt)
    obs.emit(pid, "match_audition", {
        "family_id": family_id, "mapping_id": match_id,
        "start_s": start_s, "end_s": end_s,
        "measurements": {"preview_duration_s": duration},
    })
    return receipt

def record_render_review(pid, family_id, render_id, verdict):
    from .family_actions import record_render_review as action
    return action(pid, family_id, render_id, verdict)
