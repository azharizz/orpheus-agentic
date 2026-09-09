"""Server-rendered dashboard panels for frames that cannot run scripts."""
def _esc(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

def _frame(width, height, body, caption):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" height="100%" preserveAspectRatio="none" '
        f'style="display:block;background:#101215">{body}</svg>'
        f'<div style="font:400 11px/1.4 system-ui;color:#8A9099;padding:4px 2px">'
        f'{_esc(caption)}</div>'
    )

def soundwave_svg(peaks, duration, part_start, part_end, zoom_peaks, zoom_bin):
    """Draw the whole film above the selected Part, without in-frame scripting."""
    width, height = 1000, 150
    body = []
    if duration > 0 and part_end > part_start:
        a = max(0.0, part_start / duration) * width
        b = min(1.0, part_end / duration) * width
        body.append(
            f'<rect x="{a:.1f}" y="0" width="{max(2.0, b - a):.1f}" height="{height}" '
            f'fill="rgba(255,90,54,0.16)"/>'
        )
    body.append(
        f'<line x1="0" y1="{height / 2}" x2="{width}" y2="{height / 2}" stroke="#2A2E33"/>'
    )
    mid = height / 2
    count = len(peaks) or 1
    step = width / count
    for index, peak in enumerate(peaks):
        tall = max(0.5, min(1.0, peak) * mid * 0.95)
        body.append(
            f'<rect x="{index * step:.2f}" y="{mid - tall:.2f}" '
            f'width="{max(0.6, step - 0.4):.2f}" height="{tall * 2:.2f}" fill="#FF5A36"/>'
        )
    caption = (
        f"Whole film · {round(duration)} s at "
        f"{duration / max(1, count):.2f} s per bin"
    )
    top = _frame(width, height, "".join(body), caption)

    zbody = [f'<line x1="0" y1="{mid}" x2="{width}" y2="{mid}" stroke="#2A2E33"/>']
    zcount = len(zoom_peaks) or 1
    zstep = width / zcount
    for index, peak in enumerate(zoom_peaks):
        tall = max(0.5, min(1.0, peak) * mid * 0.95)
        zbody.append(
            f'<rect x="{index * zstep:.2f}" y="{mid - tall:.2f}" '
            f'width="{max(0.6, zstep - 0.4):.2f}" height="{tall * 2:.2f}" fill="#FF5A36"/>'
        )
    zcaption = (
        f"Selected Part · {part_start:g}–{part_end:g} s at {zoom_bin:.3f} s per bin"
    )
    bottom = _frame(width, height, "".join(zbody), zcaption)
    return (
        '<div style="height:100%;display:flex;flex-direction:column;gap:2px">'
        + top
        + bottom
        + "</div>"
    )

def matcher_svg(proposals, densities, duration):
    """Each proposal at its position in the film, height showing similarity."""
    width, height = 1000, 240
    base = height - 18
    body = []
    if densities:
        peak = max(densities) or 1
        step = width / len(densities)
        for index, value in enumerate(densities):
            tall = value / peak * (base - 10)
            body.append(
                f'<rect x="{index * step:.2f}" y="{base - tall:.2f}" '
                f'width="{max(0.6, step):.2f}" height="{tall:.2f}" fill="#4A4F55"/>'
            )
    for item in proposals:
        if duration <= 0:
            break
        x = min(1.0, item["t"] / duration) * width
        tall = max(2.0, min(1.0, item["s"]) * (base - 12))
        colour = "#9DCFAD" if item["k"] == "accepted" else "#EAC17C"
        body.append(
            f'<rect x="{x - 1.5:.2f}" y="{base - tall:.2f}" width="3" '
            f'height="{tall:.2f}" fill="{colour}"/>'
        )
    body.append(f'<line x1="0" y1="{base}" x2="{width}" y2="{base}" stroke="#2A2E33"/>')
    scores = [item["s"] for item in proposals]
    caption = (
        f"{len(proposals)} proposals · similarity "
        f"{min(scores):.3f}–{max(scores):.3f} · {sum(densities)} detected events"
        if scores
        else "No scored proposals yet · similarity is ranking evidence, never approval"
    )
    head = (
        '<div style="font:600 14px/1.2 system-ui;color:#E6E8EA;padding:2px">'
        f"{len(proposals)} proposals across {round(duration)} s</div>"
    )
    legend = (
        '<div style="display:flex;gap:12px;font:400 10px/1.4 system-ui;padding:2px">'
        '<span style="color:#EAC17C">▮ proposal (height = similarity)</span>'
        '<span style="color:#9DCFAD">▮ accepted</span>'
        '<span style="color:#4A4F55">▮ detected events</span></div>'
    )
    return (
        '<div style="height:100%;display:flex;flex-direction:column">'
        + head
        + _frame(width, height, "".join(body), caption)
        + legend
        + "</div>"
    )

def now_svg(bands, part_start, part_end, duration):
    """Show which families claim the selected Part, and which remain unresolved."""
    width, height = 1000, 120
    body = []
    span = max(1e-6, part_end - part_start)
    for band in bands:
        start = max(part_start, band["s"])
        end = min(part_end, band["e"])
        if end <= start:
            continue
        x = (start - part_start) / span * width
        wide = max(2.0, (end - start) / span * width)
        colour = "#9DCFAD" if band["k"] == "accepted" else "#EAC17C"
        body.append(
            f'<rect x="{x:.2f}" y="20" width="{wide:.2f}" height="{height - 44}" '
            f'fill="{colour}" fill-opacity="0.55"/>'
        )
    body.append(
        f'<line x1="{width / 2}" y1="0" x2="{width / 2}" y2="{height}" stroke="#E6E8EA" '
        f'stroke-width="1.5"/>'
    )
    inside = sum(1 for b in bands if b["e"] > part_start and b["s"] < part_end)
    caption = (
        f"{inside} claimed range(s) inside {part_start:g}–{part_end:g} s. "
        "Unclaimed time is unreviewed, rather than proven silent; this is never an approval."
    )
    return _frame(width, height, "".join(body), caption)


def player_svg(video_url, peaks, duration, part_start, part_end):
    """Put the picture and its soundwave in one frame so they share a clock."""
    width, height = 1000, 130
    mid = height / 2
    body = []
    if duration > 0 and part_end > part_start:
        a = max(0.0, part_start / duration) * width
        b = min(1.0, part_end / duration) * width
        body.append(
            f'<rect x="{a:.1f}" y="0" width="{max(2.0, b - a):.1f}" '
            f'height="{height}" fill="rgba(255,90,54,0.16)"/>'
        )
    body.append(f'<line x1="0" y1="{mid}" x2="{width}" y2="{mid}" stroke="#2A2E33"/>')
    count = len(peaks) or 1
    step = width / count
    for index, peak in enumerate(peaks):
        tall = max(0.5, min(1.0, peak) * mid * 0.95)
        body.append(
            f'<rect x="{index * step:.2f}" y="{mid - tall:.2f}" '
            f'width="{max(0.6, step - 0.4):.2f}" height="{tall * 2:.2f}" fill="#FF5A36"/>'
        )
    return (
        '<div style="height:100%;display:flex;flex-direction:column;gap:4px">'
        f'<video src="{_esc(video_url)}#t={part_start:g}" controls preload="metadata" '
        'style="width:100%;flex:1;min-height:0;background:#000"></video>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" height="90" preserveAspectRatio="none" '
        f'style="display:block;background:#101215;flex:0 0 auto">{"".join(body)}</svg>'
        '<div style="font:400 11px/1.4 system-ui;color:#8A9099;flex:0 0 auto">'
        f'Whole film · {round(duration)} s · Part {part_start:g}–{part_end:g} s shaded'
        '</div></div>'
    )
