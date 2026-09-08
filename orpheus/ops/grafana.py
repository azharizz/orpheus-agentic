"""Setup, export and verify the local stack. No cloud credentials required."""

import argparse
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from ..config import GRAFANA_PORTS, OBSERVABILITY_ASSETS, SERVER_PORT
from . import observability as obs

DATASOURCE_UIDS = {"orpheus-prometheus", "orpheus-loki", "orpheus-tempo"}
CLOUD_DATASOURCE_UIDS = {"grafanacloud-prom", "grafanacloud-logs", "grafanacloud-traces"}


def compose(*args):
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(obs.STORE / ".env"),
            "-f",
            str(OBSERVABILITY_ASSETS / "compose.yaml"),
            *args,
        ],
        check=True,
    )


def setup():
    dashboard()
    obs.STORE.mkdir(parents=True, exist_ok=True)
    envpath = obs.STORE / ".env"
    if envpath.exists():
        values = dict(
            line.split("=", 1)
            for line in envpath.read_text().splitlines()
            if "=" in line
        )
    else:
        values = {
            "GRAFANA_ADMIN_PASSWORD": secrets.token_urlsafe(30),
            "MCP_CALLER_TOKEN": secrets.token_urlsafe(30),
        }

    def save():
        envpath.write_text("\n".join(k + "=" + v for k, v in values.items()) + "\n")
        envpath.chmod(0o600)

    values.update(
        {"ORPHEUS_" + name + "_PORT": str(port) for name, port in GRAFANA_PORTS.items()}
    )
    values["ORPHEUS_OBSERVABILITY_DIR"] = str(obs.STORE.resolve())
    template = (OBSERVABILITY_ASSETS / "prometheus.yaml").read_text()
    (obs.STORE / "prometheus.yaml").write_text(
        template.replace("__METRICS_PORT__", str(GRAFANA_PORTS["METRICS"]))
    )
    save()
    compose("up", "-d", "grafana", "loki", "tempo", "prometheus")
    with httpx.Client(
        base_url=f"http://127.0.0.1:{GRAFANA_PORTS['GRAFANA']}",
        auth=("admin", values["GRAFANA_ADMIN_PASSWORD"]),
        timeout=10,
        trust_env=False,
    ) as client:
        for attempt in range(45):
            try:
                if client.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        # Grafana persists its admin credential in the Docker volume. Align it
        # with the active data directory before creating scoped MCP credentials.
        compose(
            "exec", "-T", "grafana", "grafana", "cli", "admin",
            "reset-admin-password", values["GRAFANA_ADMIN_PASSWORD"],
        )
        if not values.get("GRAFANA_MCP_TOKEN"):
            response = client.post(
                "/api/serviceaccounts",
                json={"name": "orpheus-mcp-reader", "role": "Viewer"},
            )
            if response.status_code == 400:
                existing = client.get(
                    "/api/serviceaccounts/search",
                    params={"query": "orpheus-mcp-reader"},
                )
                existing.raise_for_status()
                ident = next(
                    row["id"]
                    for row in existing.json().get("serviceAccounts", [])
                    if row.get("name") == "orpheus-mcp-reader"
                )
            else:
                response.raise_for_status()
                ident = response.json()["id"]
            response = client.post(
                f"/api/serviceaccounts/{ident}/tokens",
                json={"name": "local-runtime-" + secrets.token_hex(4)},
            )
            response.raise_for_status()
            values["GRAFANA_MCP_TOKEN"] = response.json()["key"]
            save()
    obs.CONFIG.write_text(
        json.dumps(
            {
                "loki_url": f"http://127.0.0.1:{GRAFANA_PORTS['LOKI']}",
                "tempo_url": f"http://127.0.0.1:{GRAFANA_PORTS['OTLP']}",
                "dashboard_url": f"http://127.0.0.1:{GRAFANA_PORTS['GRAFANA']}/d/orpheus/agentic-foley-control-room",
                "mcp_url": f"http://127.0.0.1:{GRAFANA_PORTS['MCP']}/mcp",
                "mcp_token": values["MCP_CALLER_TOKEN"],
            }
        )
    )
    obs.CONFIG.chmod(0o600)
    compose("up", "-d", "mcp")
    print(
        f"Grafana: http://127.0.0.1:{GRAFANA_PORTS['GRAFANA']}/d/orpheus/agentic-foley-control-room · credentials stored privately."
    )


def dashboard():
    panels = []
    orange, green, amber, rose = "#FF5A36", "#9DCFAD", "#EAC17C", "#FF8790"
    cfg = obs.config() or {}
    prom = cfg.get("prometheus_datasource_uid") or "orpheus-prometheus"
    loki = cfg.get("loki_datasource_uid") or "orpheus-loki"
    base = '{service_name="orpheus"} | json | project_id=~"$project"'
    part = (
        '{service_name="orpheus",event=~"family_range|sound_event|candidate|'
        'candidate_timing_measured|human_review|selection|match_audition|movie_noise"}'
        ' | json | project_id=~"$project"'
        " | start_s >= $part_start | start_s <= $part_end"
    )
    app = f"http://127.0.0.1:{SERVER_PORT}"

    def target(expr, ref="A", datasource=prom, *, instant=False, legend=None):
        source_type = "loki" if datasource == loki else "prometheus"
        row = {
            "refId": ref,
            "expr": expr,
            "datasource": {"type": source_type, "uid": datasource},
            "editorMode": "code",
        }
        if instant:
            row.update(instant=True, range=False)
        elif datasource == prom:
            row.update(format="time_series", instant=False, range=True)
        if legend:
            row["legendFormat"] = legend
        if datasource == loki:
            row["queryType"] = "instant" if instant else "range"
            row["maxLines"] = 500
        return row

    counter = [0]

    def add(title, kind, grid, targets, *, description="", options=None,
            field=None, transformations=None, overrides=None, datasource=None):
        panel = {
            "id": next_id(),
            "title": title,
            "type": kind,
            "gridPos": grid,
            "description": description,
            "datasource": (
                {"type": "prometheus" if datasource == prom else "loki", "uid": datasource}
                if datasource
                else {"type": "datasource", "uid": "-- Mixed --"}
            ),
            "targets": targets,
            "options": options or {},
            "fieldConfig": {"defaults": field or {}, "overrides": overrides or []},
        }
        if transformations:
            panel["transformations"] = transformations
        panels.append(panel)
        return panel

    def next_id():
        counter[0] += 1
        return counter[0]

    stat_options = {
        "colorMode": "value",
        "graphMode": "none",
        "justifyMode": "auto",
        "orientation": "horizontal",
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "textMode": "auto",
    }
    safe_thresholds = {
        "mode": "absolute",
        "steps": [{"color": green, "value": None}, {"color": rose, "value": 1}],
    }
    project = '{project_id=~"$project"}'
    gauge_options = {
        "displayMode": "gradient",
        "minVizHeight": 16,
        "minVizWidth": 8,
        "orientation": "auto",
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "showThresholdLabels": False,
        "showThresholdMarkers": True,
        "sizing": "auto",
        "valueMode": "color",
    }
    table_options = {
        "cellHeight": "sm",
        "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        "showHeader": True,
    }

    add(
        "Is this film's sound finished?", "stat", {"x": 0, "y": 0, "w": 24, "h": 3},
        [
            target('sum(orpheus_family_ranges' + project[:-1] + ',status="pending"})', "A", instant=True, legend="Awaiting your review"),
            target('sum(orpheus_family_ranges' + project[:-1] + ',status="accepted"})', "B", instant=True, legend="Accepted"),
            target('sum(orpheus_family_ranges' + project[:-1] + ',status="rejected"})', "C", instant=True, legend="Rejected"),
        ],
        description="The one question this dashboard answers. Pending are ranked proposals, never decisions. Zero pending does not mean the sound is right; only you can judge that by listening.",
        options={**stat_options, "colorMode": "background", "textMode": "value_and_name"},
        field={"color": {"mode": "thresholds"}, "decimals": 0, "noValue": "0",
               "thresholds": {"mode": "absolute", "steps": [{"color": green, "value": None}]}},
        overrides=[
            {"matcher": {"id": "byName", "options": "Awaiting your review"},
             "properties": [{"id": "thresholds", "value": {"mode": "absolute", "steps": [
                 {"color": green, "value": None}, {"color": amber, "value": 1}]}}]},
            {"matcher": {"id": "byName", "options": "Rejected"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#3A3F45"}}]},
        ],
    )
    add(
        "The film", "text", {"x": 0, "y": 3, "w": 14, "h": 11}, [],
        description="The picture this project replaces sound inside. Every panel around it describes this same film on the picture clock. Seek to $part_start to inspect the selected Part.",
        options={"mode": "html", "content": (
            '<video controls preload="metadata" style="width:100%;height:100%;max-height:340px;'
            'background:#050607;border-radius:2px" '
            f'src="{app}/projects/${{project}}/video.mp4#t=${{part_start}}"></video>'
        )},
    )
    add(
        "Last recorded run", "stat", {"x": 14, "y": 3, "w": 10, "h": 4},
        [
            target("orpheus_candidate_clipped_samples" + project, "A", instant=True, legend="Clipped samples"),
            target("orpheus_project_candidate_state" + project, "B", instant=True, legend="Candidate"),
            target("orpheus_project_review_state" + project, "C", instant=True, legend="Human verdict"),
        ],
        description="State of the LAST recorded run in the selected time range, not a live check of the render you are watching. Clipped samples above zero must be re-rendered. A verdict here is a past decision bound to one audio hash; it never transfers to a new render.",
        options={**stat_options, "colorMode": "background", "textMode": "value_and_name"},
        field={
            "color": {"mode": "thresholds"},
            "decimals": 0,
            "noValue": "NO RUN",
            "mappings": [{"type": "value", "options": {
                "-1": {"text": "UNSUITABLE", "color": rose},
                "0": {"text": "NOT DECIDED", "color": amber},
                "1": {"text": "PASSED LAST RUN", "color": green},
            }}],
            "thresholds": {"mode": "absolute", "steps": [{"color": amber, "value": None}]},
        },
        overrides=[{
            "matcher": {"id": "byName", "options": "Clipped samples"},
            "properties": [
                {"id": "mappings", "value": []},
                {"id": "thresholds", "value": safe_thresholds},
            ],
        }],
    )
    add(
        "Sound families in this film", "barchart", {"x": 14, "y": 7, "w": 10, "h": 7},
        [target(base + ' | event="family_range"', datasource=loki)],
        description="How many occurrences each sound family has, split by decision. Pending are proposals awaiting your review, never approvals.",
        options={"barRadius": 0, "barWidth": .7, "fullHighlight": False, "groupWidth": .8, "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "orientation": "horizontal", "showValue": "auto", "stacking": "normal", "tooltip": {"mode": "multi", "sort": "none"}, "xField": "status"},
        transformations=[
            {"id": "extractFields", "options": {"source": "Line", "format": "json", "replace": True}},
            {"id": "filterFieldsByName", "options": {"include": {"names": ["status", "mapping_id"]}}},
            {"id": "groupBy", "options": {"fields": {
                "status": {"aggregations": [], "operation": "groupby"},
                "mapping_id": {"aggregations": ["count"], "operation": "aggregate"},
            }}},
            {"id": "sortBy", "options": {"fields": [{"field": "status", "desc": False}]}},
        ],
        field={"unit": "short", "decimals": 0, "custom": {"fillOpacity": 80, "lineWidth": 0}, "links": [
            {"title": "Review these matches in Orpheus", "url": f"{app}/workspace?project=${{project}}&view=review"},
        ]},
        datasource=loki,
    )
    add(
        "Soundwave of the film", "text", {"x": 0, "y": 14, "w": 14, "h": 8}, [],
        description="Top: the whole film at coarse resolution, with the selected Part shaded. Bottom: the same audio zoomed to $part_start-$part_end s, where individual contacts become visible. Both markers follow the player. Amplitude is signal level only \u2014 a tall peak is not a footstep and a flat stretch is not proven silence.",
        options={"mode": "html", "content": (
            '<div id="owave" style="height:100%;display:flex;flex-direction:column;gap:4px;'
            'font:400 11px/1.3 system-ui;color:#8A9099">'
            '<div id="owave-note">Loading measured waveform\u2026</div>'
            '<canvas id="owave-all" style="width:100%;height:34%"></canvas>'
            '<div id="owave-zl" style="color:#6E747C"></div>'
            '<canvas id="owave-zoom" style="width:100%;flex:1;min-height:60px"></canvas>'
            '</div>'
            '<script>(function(){'
            'var note=document.getElementById("owave-note"),zl=document.getElementById("owave-zl"),'
            'ca=document.getElementById("owave-all"),cz=document.getElementById("owave-zoom");'
            'if(!ca)return;'
            'if(window.__owaveTimer)clearInterval(window.__owaveTimer);'
            'function urlv(n,fb){var m=new RegExp("[?&]var-"+n+"=([^&#]*)").exec(location.search);'
            'return m?decodeURIComponent(m[1]):fb;}'
            f'var app="{app}",pid="${{project}}";'
            'var zs=Number(urlv("part_start","${part_start}"))||0,'
            'ze=Number(urlv("part_end","${part_end}"))||0;'
            'var all=null,dur=0,zoom=null;'
            'function vid(){var v=document.querySelectorAll("video");return v.length?v[0]:null;}'
            'function paint(c,peaks,t0,t1,shade){'
            'var r=c.getBoundingClientRect(),d=window.devicePixelRatio||1;'
            'c.width=r.width*d;c.height=r.height*d;'
            'var x=c.getContext("2d");x.setTransform(d,0,0,d,0,0);x.clearRect(0,0,r.width,r.height);'
            'if(!peaks||!peaks.length)return;'
            'var mid=r.height/2,n=peaks.length,span=t1-t0;'
            'if(shade&&span>0&&ze>zs){'
            'var a=Math.max(0,(zs-t0)/span)*r.width,b=Math.min(1,(ze-t0)/span)*r.width;'
            'x.fillStyle="rgba(255,90,54,0.16)";x.fillRect(a,0,Math.max(2,b-a),r.height);}'
            'x.strokeStyle="#2A2E33";x.beginPath();x.moveTo(0,mid);x.lineTo(r.width,mid);x.stroke();'
            'x.fillStyle="#FF5A36";'
            'for(var i=0;i<n;i++){var h=Math.max(0.5,Math.min(1,peaks[i])*mid*0.95);'
            'x.fillRect(i*r.width/n,mid-h,Math.max(1,r.width/n-0.4),h*2);}'
            'var v=vid();'
            'if(v&&span>0){var ct=v.currentTime;'
            'if(ct>=t0&&ct<=t1){var px=(ct-t0)/span*r.width;'
            'x.strokeStyle="#9DCFAD";x.lineWidth=2;x.beginPath();x.moveTo(px,0);x.lineTo(px,r.height);x.stroke();}}'
            '}'
            'function draw(){paint(ca,all,0,dur,true);if(zoom)paint(cz,zoom,zs,ze,false);}'
            'var busy=false;'
            'function tick(){'
            'var a=Number(urlv("part_start",zs))||0,b=Number(urlv("part_end",ze))||0;'
            'if(b>a&&(a!==zs||b!==ze)&&!busy){busy=true;zs=a;ze=b;'
            'get(zs,Math.min(ze,dur),900).then(function(j){zoom=j.peaks||[];'
            'zl.textContent="Selected Part \u00b7 "+zs+"\u2013"+ze+" s at "'
            '+(j.bin_duration_s||0).toFixed(3)+" s per bin";busy=false;draw();})'
            '.catch(function(){busy=false;});}'
            'draw();}'
            'function get(a,b,bins){return fetch(app+"/api/waveform?project_id="+pid'
            '+"&role=original&start_s="+a+(b?"&end_s="+b:"")+"&bins="+bins)'
            '.then(function(r){if(!r.ok)throw new Error(r.status);return r.json();});}'
            'get(0,0,900).then(function(j){all=j.peaks||[];dur=j.duration_s||j.end_s||0;'
            'if(!(ze>zs)){zs=0;ze=Math.min(dur,60);}'
            'note.textContent="Whole film \u00b7 "+Math.round(dur)+" s at "'
            '+(dur/Math.max(1,all.length)).toFixed(2)+" s per bin";'
            'draw();return get(zs,Math.min(ze,dur),900);})'
            '.then(function(j){zoom=j.peaks||[];'
            'zl.textContent="Selected Part \u00b7 "+zs+"\u2013"+ze+" s at "'
            '+(j.bin_duration_s||0).toFixed(3)+" s per bin";'
            'draw();window.__owaveTimer=setInterval(tick,250);})'
            '.catch(function(e){note.textContent="Waveform unavailable ("+e.message'
            '+"). Orpheus must be running; this is not evidence of silence.";});'
            '})();</script>'
        )},
    )
    add(
        "How sure is the matcher, moment by moment", "text", {"x": 0, "y": 22, "w": 24, "h": 9}, [],
        description="Each bar is a proposed occurrence of a sound family at its real position in the film, with height showing the matcher's cosine similarity to your confirmed examples. Grey behind it is the density of detected acoustic events, which is texture, not sound identity. Similarity is ranking evidence only \u2014 never a probability, never an approval. It follows the player above.",
        options={"mode": "html", "content": (
            '<div id="osure" style="height:100%;display:flex;flex-direction:column;gap:5px;'
            'font:400 11px/1.35 system-ui;color:#8A9099">'
            '<div id="osure-head" style="font:600 14px/1.2 system-ui;color:#E6E8EA">Loading\u2026</div>'
            '<canvas id="osure-c" style="width:100%;flex:1;min-height:80px"></canvas>'
            '<div style="display:flex;gap:12px;font-size:10px;align-items:center">'
            '<span style="color:#EAC17C">\u25ae proposal (height = similarity)</span>'
            '<span style="color:#9DCFAD">\u25ae accepted</span>'
            '<span style="color:#4A4F55">\u25ae detected events</span>'
            '<span id="osure-sub" style="margin-left:auto;color:#6E747C"></span></div>'
            '</div>'
            '<script>(function(){'
            'var c=document.getElementById("osure-c"),head=document.getElementById("osure-head"),'
            'sub=document.getElementById("osure-sub");'
            'if(!c)return;'
            'if(window.__osureTimer)clearInterval(window.__osureTimer);'
            f'var app="{app}";'
            'function urlv(n,fb){var m=new RegExp("[?&]var-"+n+"=([^&#]*)").exec(location.search);'
            'return m?decodeURIComponent(m[1]):fb;}'
            'var pid=urlv("project","${project}");'
            'var props=[],dens=[],dur=0,lo=1,hi=1;'
            'function vid(){var v=document.querySelectorAll("video");return v.length?v[0]:null;}'
            'function draw(){'
            'var r=c.getBoundingClientRect(),d=window.devicePixelRatio||1;'
            'c.width=r.width*d;c.height=r.height*d;'
            'var x=c.getContext("2d");x.setTransform(d,0,0,d,0,0);'
            'x.fillStyle="#101215";x.fillRect(0,0,r.width,r.height);'
            'if(!dur){return;}'
            'var base=r.height-14;'
            'if(dens.length){var mx=Math.max.apply(null,dens)||1;x.fillStyle="#4A4F55";'
            'for(var i=0;i<dens.length;i++){var h=dens[i]/mx*base*0.55;'
            'x.fillRect(i*r.width/dens.length,base-h,Math.max(1,r.width/dens.length),h);}}'
            'var span=hi-lo||1;'
            'for(var j=0;j<props.length;j++){var m=props[j];'
            'var px=m.t/dur*r.width;'
            'var norm=(m.s-lo)/span;var h=Math.max(3,(0.18+norm*0.82)*base);'
            'x.fillStyle=m.k==="accepted"?"#9DCFAD":"#EAC17C";'
            'x.fillRect(px-1.5,base-h,3,h);}'
            'x.strokeStyle="#2A2E33";x.beginPath();x.moveTo(0,base);x.lineTo(r.width,base);x.stroke();'
            'var v=vid();'
            'if(v){var px2=Math.min(1,v.currentTime/dur)*r.width;'
            'x.strokeStyle="#E6E8EA";x.lineWidth=1.5;x.beginPath();'
            'x.moveTo(px2,0);x.lineTo(px2,base);x.stroke();'
            'var near=null,best=1e9;'
            'for(var q=0;q<props.length;q++){var dd=Math.abs(props[q].t-v.currentTime);'
            'if(dd<best){best=dd;near=props[q];}}'
            'var mm=Math.floor(v.currentTime/60),ss=("0"+Math.floor(v.currentTime%60)).slice(-2);'
            'if(near&&best<=12){head.textContent=mm+":"+ss+" \u2014 nearest proposal scores "'
            '+near.s.toFixed(3)+(near.k==="accepted"?" (accepted)":" (awaiting review)");'
            'head.style.color=near.k==="accepted"?"#9DCFAD":"#EAC17C";}'
            'else{head.textContent=mm+":"+ss+" \u2014 no proposal near this moment";'
            'head.style.color="#8A9099";}}'
            'x.fillStyle="#6E747C";x.font="10px system-ui";'
            'x.fillText("0:00",2,r.height-2);x.textAlign="right";'
            'x.fillText(Math.floor(dur/60)+" min",r.width-2,r.height-2);x.textAlign="left";'
            '}'
            'function ready(){'
            'if(props.length){var sc=props.map(function(m){return m.s;});'
            'lo=Math.min.apply(null,sc);hi=Math.max.apply(null,sc);'
            'sub.textContent=props.length+" proposals \u00b7 similarity "'
            '+lo.toFixed(3)+"\u2013"+hi.toFixed(3)+" \u00b7 "+dens.reduce(function(a,b){return a+b;},0)'
            '+" detected events";}'
            'draw();window.__osureTimer=setInterval(draw,250);}'
            'fetch(app+"/api/families?project_id="+pid).then(function(r){return r.json();})'
            '.then(function(j){var fs=Array.isArray(j)?j:(j.families||[]);'
            'fs.forEach(function(f){'
            '(f.accepted_ranges||[]).forEach(function(m){if(m.range_s&&m.similarity_score!=null)'
            'props.push({t:m.refined_anchor_s||m.range_s[0],s:m.similarity_score,k:"accepted"});});'
            '(f.pending_matches||[]).forEach(function(m){if(m.range_s&&m.similarity_score!=null)'
            'props.push({t:m.refined_anchor_s||m.range_s[0],s:m.similarity_score,k:"pending"});});});'
            'return fetch(app+"/api/movie?project_id="+pid);})'
            '.then(function(r){return r.json();})'
            '.then(function(j){dur=j.waveform&&j.waveform.length?'
            'j.waveform[j.waveform.length-1].time_s:0;'
            'var evs=j.events||[];if(!dur&&evs.length)dur=evs[evs.length-1].range_s[1];'
            'var B=120;dens=new Array(B).fill(0);'
            'evs.forEach(function(e){var t=e.anchor_s||e.range_s[0];'
            'var b=Math.min(B-1,Math.max(0,Math.floor(t/dur*B)));dens[b]++;});'
            'ready();})'
            '.catch(function(e){head.textContent="Matcher evidence unavailable";'
            'head.style.color="#FF8790";'
            'sub.textContent="Orpheus must be running ("+e.message+"). Not evidence that no proposals exist.";});'
            '})();</script>'
        )},
    )
    add(
        "What is happening at this moment", "text", {"x": 14, "y": 14, "w": 10, "h": 8}, [],
        description="The picture window around the playhead, with every sound-family decision that covers it. Green accepted, amber awaiting your review. Ticks mark where a replacement sound was placed. It follows the player above. A pending band is a ranked proposal, never an approval, and an empty stretch is unexamined rather than proven silent.",
        options={"mode": "html", "content": (
            '<div id="onow" style="height:100%;display:flex;flex-direction:column;gap:5px;'
            'font:400 11px/1.35 system-ui;color:#8A9099">'
            '<div id="onow-head" style="font:600 15px/1.2 system-ui;color:#E6E8EA">Loading\u2026</div>'
            '<div id="onow-sub"></div>'
            '<canvas id="onow-c" style="width:100%;flex:1;min-height:70px"></canvas>'
            '<div id="onow-legend" style="display:flex;gap:12px;font-size:10px;align-items:center">'
            '<span style="color:#9DCFAD">\u25a0 accepted</span>'
            '<span style="color:#EAC17C">\u25a0 awaiting review</span>'
            '<span style="color:#FF5A36">\u2502 placed contact</span>'
            '<span style="color:#8A9099">\u2502 playhead</span>'
            '<span id="onow-static" style="font-size:10px;color:#6E747C;margin-left:auto"></span></div>'
            '</div>'
            '<script>(function(){'
            'var c=document.getElementById("onow-c"),head=document.getElementById("onow-head"),'
            'sub=document.getElementById("onow-sub"),st=document.getElementById("onow-static");'
            'if(!c)return;'
            'if(window.__onowTimer)clearInterval(window.__onowTimer);'
            f'var app="{app}";'
            'function urlv(n,fb){var m=new RegExp("[?&]var-"+n+"=([^&#]*)").exec(location.search);'
            'return m?decodeURIComponent(m[1]):fb;}'
            'var pid=urlv("project","${project}");'
            'var WIN=20,bands=[],ticks=[],dur=0,fam="";'
            'function vid(){var v=document.querySelectorAll("video");return v.length?v[0]:null;}'
            'function draw(){'
            'var v=vid(),t=v?v.currentTime:0;'
            'var r=c.getBoundingClientRect(),d=window.devicePixelRatio||1;'
            'c.width=r.width*d;c.height=r.height*d;'
            'var x=c.getContext("2d");x.setTransform(d,0,0,d,0,0);x.clearRect(0,0,r.width,r.height);'
            'var t0=Math.max(0,t-WIN/2),t1=t0+WIN,span=WIN;'
            'x.fillStyle="#101215";x.fillRect(0,0,r.width,r.height);'
            'var here=[];'
            'for(var i=0;i<bands.length;i++){var b=bands[i];'
            'if(b.e<t0||b.s>t1)continue;'
            'if(t>=b.s&&t<=b.e)here.push(b);'
            'var a=Math.max(0,(b.s-t0)/span)*r.width,w=Math.max(2,((Math.min(b.e,t1)-Math.max(b.s,t0))/span)*r.width);'
            'x.fillStyle=b.k==="accepted"?"rgba(157,207,173,0.55)":"rgba(234,193,124,0.5)";'
            'x.fillRect(a,r.height*0.18,w,r.height*0.64);}'
            'x.strokeStyle="#FF5A36";x.lineWidth=2;'
            'for(var j=0;j<ticks.length;j++){var tk=ticks[j];if(tk<t0||tk>t1)continue;'
            'var px=(tk-t0)/span*r.width;x.beginPath();x.moveTo(px,r.height*0.10);x.lineTo(px,r.height*0.90);x.stroke();}'
            'x.strokeStyle="#3A3F45";x.lineWidth=1;'
            'x.beginPath();x.moveTo(0,r.height/2);x.lineTo(r.width,r.height/2);x.stroke();'
            'var mid=r.width/2;x.strokeStyle="#E6E8EA";x.lineWidth=1.5;'
            'x.beginPath();x.moveTo(mid,0);x.lineTo(mid,r.height);x.stroke();'
            'x.fillStyle="#6E747C";x.font="10px system-ui";'
            'x.fillText(t0.toFixed(1)+" s",2,r.height-2);'
            'x.textAlign="right";x.fillText(t1.toFixed(1)+" s",r.width-2,r.height-2);x.textAlign="left";'
            'var mm=Math.floor(t/60),ss=("0"+Math.floor(t%60)).slice(-2);'
            'if(here.length){var acc=here.filter(function(b){return b.k==="accepted";}).length;'
            'head.textContent=mm+":"+ss+" \u2014 "+(acc?"inside an accepted replacement":"inside a proposal awaiting your review");'
            'head.style.color=acc?"#9DCFAD":"#EAC17C";'
            'sub.textContent=fam+" \u00b7 "+here.length+" range"+(here.length>1?"s":"")+" cover this moment";}'
            'else{head.textContent=mm+":"+ss+" \u2014 no decision covers this moment";'
            'head.style.color="#8A9099";sub.textContent="Unexamined picture, not proven silent.";}'
            '}'
            'fetch(app+"/api/families?project_id="+pid).then(function(r){return r.json();})'
            '.then(function(j){var fs=Array.isArray(j)?j:(j.families||[]);'
            'fs.forEach(function(f){if(!fam)fam=f.name||f.id||"";'
            '(f.accepted_ranges||[]).forEach(function(m){if(m.range_s)bands.push({s:m.range_s[0],e:m.range_s[1],k:"accepted"});'
            'if(m.refined_anchor_s)ticks.push(m.refined_anchor_s);});'
            '(f.pending_matches||[]).forEach(function(m){if(m.range_s)bands.push({s:m.range_s[0],e:m.range_s[1],k:"pending"});});});'
            'var acc=bands.filter(function(b){return b.k==="accepted";}).length;'
            'st.textContent=bands.length+" ranges known \u00b7 "+acc+" accepted \u00b7 "'
            '+(bands.length-acc)+" awaiting review \u00b7 window \u00b1"+(WIN/2)+" s";'
            'draw();window.__onowTimer=setInterval(draw,200);})'
            '.catch(function(e){head.textContent="Decisions unavailable";head.style.color="#FF8790";'
            'sub.textContent="Orpheus must be running ("+e.message+"). This is not evidence that no decisions exist.";});'
            '})();</script>'
        )},
    )
    add(
        "What the film still hides", "table", {"x": 0, "y": 31, "w": 14, "h": 9},
        [target(base + ' | event=~"movie_analysis|movie_noise|movie_review|family_movie_search"', datasource=loki)],
        description="Findings and unreviewed regions across the whole picture. An empty table means nothing was detected, never that nothing is there.",
        options=table_options,
        transformations=[
            {"id": "extractFields", "options": {"source": "Line", "format": "json", "replace": True, "keepTime": True}},
            {"id": "filterFieldsByName", "options": {"include": {"names": ["Time", "event", "start_s", "end_s", "family_id", "status", "decision"]}}},
            {"id": "sortBy", "options": {"fields": [{"field": "start_s", "desc": False}]}},
        ],
        field={"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "links": [
            {"title": "Inspect this moment in Orpheus", "url": f"{app}/workspace?project=${{project}}&view=part&time=${{__data.fields[\"start_s\"]}}"},
        ]},
        datasource=loki,
    )


    add(
        "Who decided, and on what", "table", {"x": 14, "y": 31, "w": 10, "h": 9},
        [target(base + ' | event=~"deterministic_baseline|candidate|selection|human_review|family_review|movie_review"', datasource=loki)],
        description="Agent proposals and human verdicts in one chronology, provenance kept distinct. A measurement never becomes an approval.",
        options=table_options,
        transformations=[
            {"id": "extractFields", "options": {"source": "Line", "format": "json", "replace": True, "keepTime": True}},
            {"id": "filterFieldsByName", "options": {"include": {"names": ["Time", "event", "candidate_id", "family_id", "decision", "verdict", "status", "trace_id"]}}},
            {"id": "sortBy", "options": {"fields": [{"field": "Time", "desc": True}]}},
        ],
        field={"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
        datasource=loki,
        overrides=[{
            "matcher": {"id": "byName", "options": "trace_id"},
            "properties": [{"id": "links", "value": [
                {"title": "Open the agent trace", "url": "/explore?left=" + json.dumps(
                    {"datasource": "orpheus-tempo", "queries": [{"query": "${__value.raw}", "queryType": "traceql"}], "range": {"from": "now-14d", "to": "now"}},
                    separators=(",", ":"),
                )},
            ]}],
        }],
    )
    health_children = []
    for title, expr, field in (
        ("Grafana MCP", 'clamp_max(orpheus_project_events_total' + project[:-1] + ',event="grafana_query"}, 1)',
         {"mappings": [{"type": "value", "options": {"0": {"text": "NO EVIDENCE", "color": amber}, "1": {"text": "VERIFIED", "color": green}}}], "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": amber, "value": None}, {"color": green, "value": 1}]}}),
        ("Collector", 'up{job="orpheus"}',
         {"mappings": [{"type": "value", "options": {"0": {"text": "DOWN", "color": rose}, "1": {"text": "ONLINE", "color": green}}}], "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": rose, "value": None}, {"color": green, "value": 1}]}}),
        ("Pending exports", "orpheus_export_pending",
         {"color": {"mode": "thresholds"}, "decimals": 0, "thresholds": safe_thresholds}),
        ("Provider failures", "orpheus_project_provider_failures_total" + project,
         {"color": {"mode": "thresholds"}, "decimals": 0, "thresholds": {"mode": "absolute", "steps": [{"color": green, "value": None}, {"color": amber, "value": 1}, {"color": rose, "value": 3}]}}),
    ):
        health_children.append({
            "id": next_id(),
            "title": title,
            "type": "stat",
            "gridPos": {"x": len(health_children) * 6, "y": 41, "w": 6, "h": 4},
            "description": "Decides whether the panels above can be trusted at all. Unresolved is not a pass.",
            "datasource": {"type": "prometheus", "uid": prom},
            "targets": [target(expr, instant=True)],
            "options": stat_options,
            "fieldConfig": {"defaults": field, "overrides": []},
        })
    panels.append({
        "id": next_id(),
        "title": "Runtime · can these panels be trusted",
        "type": "row",
        "collapsed": True,
        "gridPos": {"x": 0, "y": 45, "w": 24, "h": 1},
        "panels": health_children,
    })

    raw_children = []
    for title, expr in (
        ("Candidate and sound measurements", base + ' | event=~"candidate|candidate_timing_measured|sound_event|sound_profile"'),
        ("Failures and rejected tools", base + ' | event=~".*failed|tool_result"'),
        ("Agent, MCP and trace evidence", base + ' | event=~"model_response|audio_response|grafana_query|grafana_investigation|tool_call|session_saved"'),
    ):
        detail = "Raw lines for diagnosing why a decision above looks wrong. Read only after a panel disagrees with the export."
        raw_children.append({
            "id": next_id(),
            "title": title,
            "type": "logs",
            "description": detail,
            "gridPos": {"x": 0, "y": 46 + len(raw_children) * 7, "w": 24, "h": 7},
            "datasource": {"type": "loki", "uid": loki},
            "targets": [target(expr, datasource=loki)],
            "options": {"dedupStrategy": "none", "enableLogDetails": True, "prettifyLogMessage": False, "showCommonLabels": False, "showLabels": False, "showTime": True, "sortOrder": "Descending", "wrapLogMessage": True},
        })
    panels.append({
        "id": next_id(),
        "title": "Raw evidence · open for diagnosis",
        "type": "row",
        "collapsed": True,
        "gridPos": {"x": 0, "y": 40, "w": 24, "h": 1},
        "panels": raw_children,
    })
    doc = {
        "uid": "orpheus",
        "title": "Agentic Foley Control Room",
        "schemaVersion": 40,
        "version": 8,
        "refresh": "5s",
        "time": {"from": "now-7d", "to": "now"},
        "tags": ["orpheus", "agent", "foley", "local"],
        "description": "This is a film, these are the sounds replaced inside it, and this is the proof for each one. Measurements describe the export; only the creator approves it.",
        "editable": False,
        "graphTooltip": 1,
        "links": [],
        "liveNow": True,
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m"]},
        "templating": {
            "list": [
                {
                    "name": "project",
                    "type": "textbox",
                    "label": "Project",
                    "current": {"text": ".*", "value": ".*"},
                },
                {
                    "name": "part_start",
                    "type": "textbox",
                    "label": "Part start (s)",
                    "current": {"text": "150", "value": "150"},
                },
                {
                    "name": "part_end",
                    "type": "textbox",
                    "label": "Part end (s)",
                    "current": {"text": "210", "value": "210"},
                },
                {
                    "name": "candidate",
                    "type": "textbox",
                    "label": "Candidate",
                    "current": {"text": ".*", "value": ".*"},
                },
            ]
        },
        "panels": panels,
    }
    validate(doc)
    folder = OBSERVABILITY_ASSETS / "dashboards"
    folder.mkdir(exist_ok=True)
    (folder / "foley.json").write_text(json.dumps(doc, indent=2))


def publish(url=None, token=None):
    """Upload the generated dashboard to a Grafana instance over its HTTP API."""
    cfg = obs.config() or {}
    base = (url or cfg.get("dashboard_url") or "").split("/d/")[0].rstrip("/")
    token = token or os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not base:
        raise ValueError("A Grafana base URL is required")
    if not token:
        raise ValueError("A Grafana service-account token is required")
    path = OBSERVABILITY_ASSETS / "dashboards" / "foley.json"
    committed = path.read_text() if path.exists() else None
    dashboard()
    doc = json.loads(path.read_text())
    if committed is not None:
        # Publishing substitutes hosted datasource UIDs; the committed file stays local.
        path.write_text(committed)
    doc.pop("id", None)
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        response = client.post(
            "/api/dashboards/db",
            headers={"Authorization": "Bearer " + token},
            json={"dashboard": doc, "overwrite": True, "message": "Orpheus evidence plane"},
        )
        response.raise_for_status()
        body = response.json()
    return {"status": "ok", "url": base + body.get("url", ""), "version": body.get("version")}


def check_committed():
    """Dashboard-as-code: the checked-in JSON must match what dashboard() generates."""
    path = OBSERVABILITY_ASSETS / "dashboards" / "foley.json"
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    before = path.read_text()
    dashboard()
    after = path.read_text()
    if before != after:
        path.write_text(before)
        return {"status": "drifted", "path": str(path), "hint": "Run dashboard() and commit the result."}
    return {"status": "ok", "path": str(path), "panels": len(validate(json.loads(after)))}


def validate(doc):
    """Fail at generation time rather than shipping panels that point at nothing."""
    if f"/d/{doc['uid']}/" not in obs.DASHBOARD_PATH + "/":
        raise ValueError(f"Dashboard uid {doc['uid']} does not match {obs.DASHBOARD_PATH}")
    seen = set()

    def walk(panels):
        for panel in panels:
            ident = panel.get("id")
            if ident is None:
                raise ValueError(f"Panel without id: {panel.get('title')}")
            if ident in seen:
                raise ValueError(f"Duplicate panel id {ident}: {panel.get('title')}")
            seen.add(ident)
            if panel["type"] != "row" and not panel.get("description"):
                raise ValueError(
                    f"Panel {ident} '{panel.get('title')}' states no decision it changes"
                )
            for source in [panel.get("datasource")] + [
                t.get("datasource") for t in panel.get("targets", [])
            ]:
                uid = (source or {}).get("uid")
                if uid and uid != "-- Mixed --" and uid not in DATASOURCE_UIDS | CLOUD_DATASOURCE_UIDS:
                    raise ValueError(f"Unknown datasource uid {uid} in {panel.get('title')}")
            walk(panel.get("panels", []))

    walk(doc["panels"])
    return seen


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        data = obs.metrics_text().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def collect():
    # Docker Desktop reaches the host bridge; only aggregate, redacted metrics on this port.
    server = ThreadingHTTPServer(
        ("127.0.0.1", GRAFANA_PORTS["METRICS"]), MetricsHandler
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        try:
            obs.flush()
        except Exception as exc:
            print(type(exc).__name__, flush=True)
        time.sleep(2)


def backfill():
    from ..domain.projects import PROJECTS

    count = 0
    for folder in PROJECTS.iterdir():
        if not (folder / "project.json").exists():
            continue
        path = folder / "events.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                obs.emit(
                    folder.name,
                    row["event"],
                    row,
                    row.get("turn_id", "historical"),
                    row.get("ts"),
                )
                count += 1
        for role, name in [("target", "original.wav")]:
            if (folder / name).exists():
                obs.emit(
                    folder.name,
                    "sound_profile",
                    {"role": role, "profile": obs.sound_profile(folder / name)},
                    "input",
                    (folder / name).stat().st_mtime,
                )
        for receipt in (folder / "takes").glob("*.json") if (folder / "takes").exists() else ():
            take = json.loads(receipt.read_text())
            wav = receipt.with_suffix(".wav")
            if wav.exists():
                obs.emit(folder.name, "sound_profile", {
                    "role": "source", "take_id": take["id"],
                    "family_id": take.get("family_id"),
                    "profile": obs.sound_profile(wav)}, "input", wav.stat().st_mtime)
        for receipt in (folder / "families").glob("*.json") if (folder / "families").exists() else ():
            family = json.loads(receipt.read_text())
            for status, key in (("accepted", "accepted_ranges"), ("rejected", "rejected_ranges"), ("pending", "pending_matches")):
                for row in family.get(key, []):
                    obs.emit(folder.name, "family_range", {
                        "family_id": family["id"], "mapping_id": row["id"], "status": status,
                        "start_s": row["range_s"][0], "end_s": row["range_s"][1],
                    }, "historical", receipt.stat().st_mtime)
            latest = family.get("latest_render", {})
            if latest:
                fitted = len(latest.get("arrangement", {}).get("rows", latest.get("mix", {}).get("ranges_s", [])))
                obs.emit(folder.name, "candidate", {
                    **latest, "measurements": {"accepted_events": fitted},
                }, "historical", receipt.stat().st_mtime)
                verdict = family.get("last_render_verdict")
                if verdict:
                    obs.emit(folder.name, "human_review", {
                        "candidate_id": latest["id"], "verdict": verdict,
                    }, "historical", receipt.stat().st_mtime)
    print("Historical event receipts queued:", count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["setup", "collect", "backfill", "flush", "stop", "check", "report", "publish"],
    )
    parser.add_argument("snapshot_id", nargs="?")
    args = parser.parse_args()
    if args.action == "setup":
        setup()
    elif args.action == "collect":
        collect()
    elif args.action == "backfill":
        backfill()
    elif args.action == "stop":
        compose("stop")
    elif args.action == "publish":
        print(json.dumps(publish(), indent=2))
    elif args.action == "check":
        result = check_committed()
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "ok" else 1)
    elif args.action == "report":
        if not args.snapshot_id:
            raise SystemExit("report needs a snapshot id")
        print(json.dumps(obs.static_report(args.snapshot_id), indent=2))
    else:
        print(obs.flush())
