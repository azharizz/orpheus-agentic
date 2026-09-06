"""Human judgments and assisted revisions stay separate from agent hypotheses."""

import hashlib
import json
import re
import time
import uuid

from ..ops import observability as obs
from . import arrangement, media, projects


def candidate(case, candidate_id):
    if not isinstance(candidate_id, str) or not re.fullmatch(
        r"[a-f0-9]{12}", candidate_id
    ):
        raise ValueError("Choose a rendered candidate.")
    folder = projects.project_dir(case["id"])
    result = json.loads((folder / (candidate_id + ".json")).read_text())
    audio = folder / (candidate_id + ".wav")
    if hashlib.sha256(audio.read_bytes()).hexdigest() != result["audio_sha256"]:
        raise ValueError(
            "Candidate audio has changed. Render a new alternative before review."
        )
    return result


def save_review(data):
    case = projects.load(data["project_id"])
    rendered = candidate(case, data["candidate_id"])
    verdict = data["verdict"]
    note = data.get("note", "")
    if verdict not in ("approve", "reject"):
        raise ValueError("Choose approve or reject.")
    if not isinstance(note, str) or len(note) > 500:
        raise ValueError("Review notes must be at most 500 characters.")
    review_id = uuid.uuid4().hex[:12]
    record = {
        "id": review_id,
        "candidate_id": rendered["id"],
        "audio_sha256": rendered["audio_sha256"],
        "verdict": verdict,
        "note": note,
        "provenance": "human_ui",
        "created_at": time.time(),
    }
    projects.atomic(
        projects.project_dir(case["id"]) / (review_id + "-human.json"),
        record,
    )
    obs.emit(
        case["id"],
        "human_review",
        {**record, "parent_project_id": case.get("take_parent_project_id", case["id"])},
    )
    return record


def assist(data):
    case = projects.load(data["project_id"])
    rendered = candidate(case, data["candidate_id"])
    bound = rendered["arrangement"]
    if bound["input_hashes"] != arrangement.identities(case):
        raise ValueError("Source media changed. This mapping cannot be revised.")
    rows = arrangement.validate(
        data["rows"],
        target_duration_s=case["seconds"],
        source_duration_s=len(media.read_audio(case["sfx_path"])) / media.RATE,
        target_ids=[row["target_id"] for row in bound["rows"]],
        source_ids=[row["source_id"] for row in bound["rows"]],
        require_impact_anchors=True,
    )
    for row in rows:
        for role in ("target", "source"):
            if role == "source" and row["disposition"] != "use":
                continue
            reference = bound["references"][role + ":" + row[role + "_id"]]
            start, end = row[role + "_range_s"]
            if not reference["range_s"][0] <= start <= end <= reference["range_s"][1]:
                raise ValueError("An edit exceeds its inspected evidence window.")
    revised = {
        **bound,
        "rows": rows,
        "summary": arrangement.suitability(rows),
        "provenance": "human_assisted",
        "human_approved": False,
        "parent_candidate_id": rendered["id"],
        "id": uuid.uuid4().hex[:20],
    }
    folder = projects.project_dir(case["id"])
    result = arrangement.render(case, revised, folder)
    result.update(
        provenance="human_assisted",
        cycle=0,
        hypothesis="User edited mapping; not autonomous evaluation",
    )
    projects.atomic(folder / (result["id"] + ".json"), result)
    projects.atomic(folder / (result["id"] + "-assisted.json"), result)
    obs.emit(case["id"], "candidate", result)
    return result
