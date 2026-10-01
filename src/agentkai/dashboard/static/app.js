/* agentkai dashboard SPA — vanilla JS, zero dependencies, fully offline. */
(function () {
  "use strict";

  var TOKEN = new URLSearchParams(location.search).get("token") || "";
  var view = document.getElementById("view");
  var pageTitle = document.getElementById("page-title");
  var nav = document.getElementById("nav");

  if (!TOKEN) {
    document.getElementById("denied").hidden = false;
    document.getElementById("app").style.display = "none";
    return;
  }
  // static assets (logo) live behind the same token auth
  document.getElementById("brand-logo").src =
    "static/img/logo.gif?token=" + encodeURIComponent(TOKEN);

  function q(path) {
    return path + (path.indexOf("?") >= 0 ? "&" : "?") +
      "token=" + encodeURIComponent(TOKEN);
  }

  async function api(path, opts) {
    var r = await fetch(q(path), opts || {});
    if (r.status === 401) {
      document.getElementById("denied").hidden = false;
      throw new Error("unauthorized");
    }
    if (!r.ok) throw new Error("HTTP " + r.status + " on " + path);
    return r.json();
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /* tiny markdown renderer: headings, code, bold, italic, lists, quotes */
  function md(src) {
    var lines = String(src).split("\n"), html = "", i = 0, inCode = false, inList = false;
    function closeList() { if (inList) { html += "</ul>"; inList = false; } }
    function inline(s) {
      s = esc(s);
      s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
      return s;
    }
    while (i < lines.length) {
      var L = lines[i];
      if (/^```/.test(L)) { inCode = !inCode; html += inCode ? "<pre><code>" : "</code></pre>"; i++; continue; }
      if (inCode) { html += esc(L) + "\n"; i++; continue; }
      var h = /^(#{1,3})\s+(.*)/.exec(L);
      if (h) { closeList(); html += "<h" + h[1].length + ">" + inline(h[2]) + "</h" + h[1].length + ">"; i++; continue; }
      if (/^\s*>\s?/.test(L)) { closeList(); html += "<blockquote>" + inline(L.replace(/^\s*>\s?/, "")) + "</blockquote>"; i++; continue; }
      if (/^\s*[-*]\s+/.test(L)) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += "<li>" + inline(L.replace(/^\s*[-*]\s+/, "")) + "</li>"; i++; continue;
      }
      if (/^\s*$/.test(L)) { closeList(); i++; continue; }
      closeList();
      html += "<p>" + inline(L) + "</p>"; i++;
    }
    closeList();
    return html;
  }

  function timeFmt(ts) {
    return new Date(ts * 1000).toLocaleTimeString();
  }
  function dateTimeFmt(ts) {
    return new Date(ts * 1000).toLocaleString();
  }

  /* ---------------- router ---------------- */
  var TITLES = { chat: "Chat", runs: "Runs", memory: "Memory",
                 scheduler: "Scheduler", settings: "Settings" };
  var currentES = null;
  function closeStream() {
    if (currentES) { try { currentES.close(); } catch (e) {} currentES = null; }
  }

  function route() {
    closeStream();
    var h = location.hash || "#/chat";
    var m = h.match(/^#\/(\w+)(?:\/(.+))?$/);
    var page = (m && TITLES[m[1]]) ? m[1] : "chat";
    var arg = m && m[2];
    pageTitle.textContent = TITLES[page];
    Array.prototype.forEach.call(nav.querySelectorAll("a"), function (a) {
      a.classList.toggle("active", a.getAttribute("data-route") === page);
    });
    ({ chat: renderChat, runs: renderRuns, memory: renderMemory,
       scheduler: renderScheduler, settings: renderSettings })[page](arg);
  }
  window.addEventListener("hashchange", route);

  /* ---------------- chat ---------------- */
  var chatRuns = []; // {id, prompt} this session
  var modelChoices = null; // [{alias, resolved}] from /api/models

  function loadModels(cb) {
    if (modelChoices) return cb(modelChoices);
    api("/api/models").then(function (d) {
      modelChoices = d.models;
      cb(modelChoices, d.default);
    }).catch(function () { cb([]); });
  }

  function renderChat() {
    view.innerHTML =
      '<div class="chat-wrap">' +
      '<div class="chat-log" id="chat-log">' +
      '<div class="empty"><div class="big">Ask agentkai anything</div>' +
      '<div>Replies stream in live, with tool calls shown inline.</div></div>' +
      "</div>" +
      '<form class="chat-input" id="chat-form">' +
      '<select id="chat-model" class="model-pick" title="Model"></select>' +
      '<input id="chat-text" placeholder="Ask agentkai…" autocomplete="off">' +
      '<button class="btn" type="submit" id="chat-send">Send</button>' +
      "</form></div>";
    var log = document.getElementById("chat-log");
    var form = document.getElementById("chat-form");
    var input = document.getElementById("chat-text");
    var sendBtn = document.getElementById("chat-send");
    var modelSel = document.getElementById("chat-model");
    loadModels(function (models, def) {
      modelSel.innerHTML = models.map(function (m) {
        return '<option value="' + esc(m.alias) + '"' +
          (m.alias === def ? " selected" : "") + ">" +
          esc(m.alias) + "</option>";
      }).join("") || '<option value="claude">claude</option>';
      modelSel.title = models.length
        ? models.map(function (m) { return m.alias + " → " + m.resolved; }).join("\n")
        : "model aliases";
    });

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      var prompt = input.value.trim();
      if (!prompt) return;
      input.value = "";
      sendBtn.disabled = true;
      addMsg(log, "user", esc(prompt));
      var bubble = addMsg(log, "assistant", "", true);
      try {
        var res = await api("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: prompt, model: modelSel.value }),
        });
        chatRuns.unshift({ id: res.run_id, prompt: prompt });
        streamRun(log, bubble, res.run_id, function () { sendBtn.disabled = false; });
      } catch (err) {
        bubble.innerHTML = '<span class="muted">error: ' + esc(err.message) + "</span>";
        sendBtn.disabled = false;
      }
    });
    input.focus();
  }

  function addMsg(log, role, html, streaming) {
    log.querySelector(".empty") && log.querySelector(".empty").remove();
    var d = document.createElement("div");
    d.className = "msg " + role;
    d.innerHTML = '<div class="bubble' + (streaming ? " caret" : "") + '">' +
      '<div class="role">' + role + "</div>" +
      '<div class="msg-body">' + html + "</div></div>";
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d.querySelector(".msg-body");
  }

  function streamRun(log, bubble, runId, onDone) {
    var text = "";
    var cards = []; // tool cards persist across later text deltas
    function render() {
      var html = esc(text);
      for (var i = 0; i < cards.length; i++) html += cards[i];
      bubble.innerHTML = html;
    }
    var es = new EventSource(q("/api/events?run_id=" + encodeURIComponent(runId)));
    currentES = es;
    es.onmessage = function (ev) {
      var e = JSON.parse(ev.data);
      if (e.type === "message_delta") {
        text += e.data.text || "";
        render();
        log.scrollTop = log.scrollHeight;
      } else if (e.type === "message") {
        text += e.data.text || "";
        render();
        log.scrollTop = log.scrollHeight;
      } else if (e.type === "approval") {
        approvalCard(log, runId, e.data);
      } else if (e.type === "tool_call") {
        text += "\n";
        cards.push(toolCard(e.data, false));
        render();
      } else if (e.type === "tool_result") {
        if (cards.length) cards[cards.length - 1] = toolCard(e.data, true);
        render();
      } else if (e.type === "run_finished" || e.type === "error") {
        es.close(); currentES = null;
        bubble.parentElement.classList.remove("caret");
        if (e.type === "error")
          bubble.innerHTML += '<div class="muted">error: ' + esc(e.data.message || "run failed") + "</div>";
        onDone && onDone();
      }
    };
    es.onerror = function () {
      es.close(); currentES = null;
      bubble.parentElement.classList.remove("caret");
      onDone && onDone();
    };
  }

  function toolCard(d, done) {
    var body = done
      ? esc((d.output || "").slice(0, 1200))
      : '<span class="tc-args">' + esc(JSON.stringify(d.args || {})) + "</span>";
    return '<div class="tool-card"><div class="tc-head"><span>⚙ ' +
      esc(d.tool || "?") + "</span><span>" + (done ? (d.ok ? "ok" : "failed") : d.risk || "") +
      "</span></div><div class=\"tc-body\">" + body + "</div></div>";
  }

  /* permission-gate approval cards: rendered for "pending" approvals,
     resolved in place when the decision event arrives */
  function approvalCard(log, runId, d) {
    var id = "appr-" + (d.action_id || Math.random().toString(36).slice(2));
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("div");
      el.className = "msg approval";
      el.id = id;
      log.appendChild(el);
    }
    if (d.decision === "pending") {
      el.innerHTML = '<div class="bubble"><div class="role">approval needed</div>' +
        '<div class="msg-body"><div class="appr-head">⚠ <strong>' + esc(d.tool || "?") +
        '</strong> <span class="muted mono">[' + esc(d.risk || "") + "]</span></div>" +
        '<div class="tc-args mono">' + esc(JSON.stringify(d.args || {})) + "</div>" +
        '<div class="appr-btns"><button class="btn appr-yes">Approve</button>' +
        '<button class="btn appr-no">Deny</button></div></div></div>';
      log.scrollTop = log.scrollHeight;
      el.querySelector(".appr-yes").addEventListener("click", function () {
        resolveApproval(el, runId, d.action_id, true);
      });
      el.querySelector(".appr-no").addEventListener("click", function () {
        resolveApproval(el, runId, d.action_id, false);
      });
    } else {
      var badge = d.decision === "allow"
        ? '<span class="status-pill done">approved</span>'
        : '<span class="status-pill error">denied</span>';
      el.innerHTML = '<div class="bubble"><div class="role">approval</div>' +
        '<div class="msg-body">' + badge + ' <strong>' + esc(d.tool || "?") + "</strong></div></div>";
    }
  }

  function resolveApproval(el, runId, actionId, approved) {
    var btns = el.querySelectorAll("button");
    Array.prototype.forEach.call(btns, function (b) { b.disabled = true; });
    api("/api/runs/" + encodeURIComponent(runId) + "/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_id: actionId, approved: approved }),
    }).catch(function (err) {
      el.querySelector(".msg-body").innerHTML +=
        '<div class="muted">approval failed: ' + esc(err.message) + "</div>";
    });
  }

  /* ---------------- runs ---------------- */
  function renderRuns(runId) {
    if (runId) return renderRunDetail(runId);
    view.innerHTML = '<div id="runs-list"><div class="empty"><div class="big">Loading runs…</div></div></div>';
    api("/api/runs").then(function (d) {
      var el = document.getElementById("runs-list");
      if (!d.runs.length) {
        el.innerHTML = '<div class="empty"><div class="big">No runs yet</div>' +
          '<div>Start one from <a class="link" href="#/chat">Chat</a> or wait for a scheduled job.</div></div>';
        return;
      }
      el.innerHTML = '<div class="card"><table class="tbl"><thead><tr>' +
        "<th>Run</th><th>Prompt</th><th>Model</th><th>Status</th><th>Started</th>" +
        "</tr></thead><tbody>" + d.runs.map(function (r) {
          return "<tr><td class='mono'><a class='link' href='#/runs/" + r.id + "'>" +
            esc(r.id.slice(0, 8)) + "</a></td><td>" + esc(r.prompt.slice(0, 80)) +
            "</td><td class='mono muted'>" + esc(shortModel(r.model)) + "</td>" +
            "<td>" + statusPill(r.status) + "</td>" +
            "<td class='muted'>" + dateTimeFmt(r.created_at) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }).catch(function (e) { view.innerHTML = errHtml(e); });
  }

  function renderRunDetail(runId) {
    view.innerHTML = '<div class="run-head"><a class="link" href="#/runs">← all runs</a>' +
      '<h2 class="mono">' + esc(runId) + '</h2><span id="run-status"></span></div>' +
      '<div class="timeline" id="timeline"><div class="empty"><div class="big">Loading events…</div></div></div>';
    Promise.all([api("/api/runs/" + encodeURIComponent(runId)),
                 api("/api/runs/" + encodeURIComponent(runId) + "/events")])
      .then(function (res) {
        var run = res[0], evs = res[1].events;
        document.getElementById("run-status").innerHTML = statusPill(run.status);
        var tl = document.getElementById("timeline");
        if (!evs.length) {
          tl.innerHTML = '<div class="empty"><div class="big">No events yet</div></div>';
          return;
        }
        tl.innerHTML = evs.map(tlItem).join("");
        if (run.status === "running") {
          // follow live
          var es = new EventSource(q("/api/events?run_id=" + encodeURIComponent(runId)));
          currentES = es;
          es.onmessage = function (ev) {
            var e = JSON.parse(ev.data);
            if (e.type === "run_finished") {
              document.getElementById("run-status").innerHTML = statusPill("done");
              es.close(); currentES = null;
            }
            var tmp = document.createElement("div");
            tmp.innerHTML = tlItem(e);
            tl.appendChild(tmp.firstChild);
          };
        }
      }).catch(function (e) { view.innerHTML = errHtml(e); });
  }

  function tlItem(e) {
    var body = "";
    if (e.type === "message_delta" || e.type === "message")
      body = '<div class="tl-text">' + esc(e.data.text || "") + "</div>";
    else if (e.type === "tool_call" || e.type === "tool_result")
      body = toolCard(e.data, e.type === "tool_result");
    else
      body = "<pre>" + esc(JSON.stringify(e.data, null, 1).slice(0, 800)) + "</pre>";
    return '<div class="tl-item t-' + esc(e.type) + '"><div class="tl-card">' +
      '<div class="tl-type">' + esc(e.type) + " · " + timeFmt(e.ts) + "</div>" +
      body + "</div></div>";
  }

  function statusPill(s) {
    var cls = s === "running" ? "running" : (s === "done" ? "done" : "error");
    return '<span class="status-pill ' + cls + '">' + esc(s) + "</span>";
  }
  function shortModel(m) { return String(m || "").split("/").pop(); }

  /* ---------------- memory ---------------- */
  function renderMemory() {
    view.innerHTML =
      '<div class="mem-layout"><div class="card"><h3>Files</h3>' +
      '<div class="file-list" id="mem-files"></div></div>' +
      '<div class="card"><h3 id="mem-title">Select a file</h3>' +
      '<div class="md-body" id="mem-body"><p class="muted">Pick a memory file to read it.</p></div></div></div>';
    api("/api/memory").then(function (d) {
      var list = document.getElementById("mem-files");
      list.innerHTML = "";
      d.files.forEach(function (f) {
        var b = document.createElement("button");
        b.textContent = (f.exists ? "" : "○ ") + f.name;
        b.style.opacity = f.exists ? 1 : 0.45;
        b.addEventListener("click", function () {
          Array.prototype.forEach.call(list.children, function (c) { c.classList.remove("active"); });
          b.classList.add("active");
          document.getElementById("mem-title").textContent = f.name;
          var body = document.getElementById("mem-body");
          if (!f.exists) {
            body.innerHTML = '<p class="muted">Not created yet — the agent writes this file on first run.</p>';
            return;
          }
          body.innerHTML = '<p class="muted">Loading…</p>';
          api("/api/memory/" + encodeURIComponent(f.name)).then(function (r) {
            document.getElementById("mem-body").innerHTML = md(r.content) || '<p class="muted">(empty)</p>';
          }).catch(function (e) {
            document.getElementById("mem-body").innerHTML = '<p class="muted">' + esc(e.message) + "</p>";
          });
        });
        list.appendChild(b);
      });
      if (list.children[0]) list.children[0].click();
    }).catch(function (e) { view.innerHTML = errHtml(e); });
  }

  /* ---------------- scheduler ---------------- */
  function renderScheduler() {
    view.innerHTML = '<div id="sched"><div class="empty"><div class="big">Loading jobs…</div></div></div>';
    api("/api/scheduler/jobs").then(function (d) {
      var el = document.getElementById("sched");
      var rows = d.jobs.map(function (j) {
        return "<tr><td><strong>" + esc(j.name) + "</strong></td>" +
          "<td class='mono'>" + esc(j.schedule) + "</td>" +
          "<td class='mono'>" + esc(j.job_type) + "</td>" +
          "<td class='mono muted'>" + esc((j.prompt || j.command || "").slice(0, 90)) + "</td>" +
          "<td>" + (j.enabled
            ? '<span class="status-pill done">on</span>'
            : '<span class="status-pill error">off</span>') + "</td>" +
          "<td class='mono muted'>" + (j.last_run ? esc(j.last_run.slice(0, 16).replace("T", " ")) : "—") + "</td>" +
          "<td><button class='btn-mini' data-run='" + esc(j.name) + "'>Run now</button> " +
          "<button class='btn-mini danger' data-del='" + esc(j.name) + "'>Delete</button></td></tr>";
      }).join("");
      el.innerHTML =
        '<div class="card"><h3>Add job</h3>' +
        '<form id="job-form" class="form-grid">' +
        '<label>Name<input id="jf-name" required maxlength="128" placeholder="morning-brief"></label>' +
        '<label>Schedule<input id="jf-schedule" required placeholder="*/15 * * * *  or  @at:2026-10-05T09:00"></label>' +
        '<label>Type<select id="jf-type"><option value="cron">cron</option><option value="at">at (one-shot)</option>' +
        '<option value="heartbeat">heartbeat</option><option value="dreaming">dreaming</option></select></label>' +
        '<label class="wide">Prompt (agent job)<input id="jf-prompt" placeholder="what the agent should do"></label>' +
        '<label class="wide">…or shell command (legacy)<input id="jf-command" placeholder="echo hi"></label>' +
        '<div class="wide"><button type="submit" class="btn-mini">Save job</button> ' +
        '<span id="jf-err" class="err"></span></div></form></div>' +
        '<div class="card"><table class="tbl"><thead><tr>' +
        "<th>Job</th><th>Schedule</th><th>Type</th><th>Prompt / command</th><th>Enabled</th><th>Last run</th><th>Actions</th>" +
        "</tr></thead><tbody>" + (rows || '<tr><td colspan="7" class="muted">No scheduled jobs yet.</td></tr>') +
        "</tbody></table></div>" +
        '<div class="muted mono">db: ' + esc(d.db) + "</div>";
      bindSchedulerForm();
      bindSchedulerActions();
    }).catch(function (e) { view.innerHTML = errHtml(e); });
  }

  function schedulerPayload() {
    return {
      name: document.getElementById("jf-name").value.trim(),
      schedule: document.getElementById("jf-schedule").value.trim(),
      job_type: document.getElementById("jf-type").value,
      prompt: document.getElementById("jf-prompt").value,
      command: document.getElementById("jf-command").value
    };
  }

  function bindSchedulerForm() {
    var form = document.getElementById("job-form");
    if (!form) return;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var err = document.getElementById("jf-err");
      err.textContent = "";
      fetch(q("/api/scheduler/jobs"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(schedulerPayload())
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (d) {
          throw new Error(d.detail || ("HTTP " + r.status));
        });
        return r.json();
      }).then(function () { renderScheduler(); })
        .catch(function (e) { err.textContent = e.message; });
    });
  }

  function bindSchedulerActions() {
    var el = document.getElementById("sched");
    el.addEventListener("click", function (ev) {
      var t = ev.target;
      if (t.dataset.run) {
        t.disabled = true;
        t.textContent = "Running…";
        fetch(q("/api/scheduler/jobs/" + encodeURIComponent(t.dataset.run) + "/run"),
          { method: "POST" })
          .then(function (r) {
            if (!r.ok) return r.json().then(function (d) {
              throw new Error(d.detail || ("HTTP " + r.status));
            });
            return r.json();
          })
          .then(function (d) { renderScheduler(); })
          .catch(function (e) {
            t.disabled = false; t.textContent = "Run now";
            alert(e.message);
          });
      } else if (t.dataset.del) {
        if (!confirm("Delete job '" + t.dataset.del + "'?")) return;
        fetch(q("/api/scheduler/jobs/" + encodeURIComponent(t.dataset.del)),
          { method: "DELETE" })
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            renderScheduler();
          })
          .catch(function (e) { alert(e.message); });
      }
    });
  }

  /* ---------------- settings ---------------- */
  function renderSettings() {
    view.innerHTML = '<div class="grid2"><div class="card"><h3>Instance</h3><dl class="kv" id="set-kv"></dl></div>' +
      '<div class="card"><h3>Security</h3><div class="md-body">' +
      "<ul><li>Bound to <code>127.0.0.1</code> only — binding anything else is refused.</li>" +
      "<li>Per-launch token required on every request (<code>?token=</code> or <code>Authorization: Bearer</code>).</li>" +
      "<li>Token printed once at startup, never logged (access log disabled).</li>" +
      "<li>No telemetry. No external requests — the UI ships zero CDN assets.</li></ul>" +
      "</div></div></div>" +
      '<div class="card"><h3>Models &amp; keys</h3><div id="model-keys">' +
      '<p class="muted">Loading…</p></div>' +
      '<p class="muted">Keys are never shown — only whether each provider is configured.</p></div>' +
      '<div class="card"><h3>API contract</h3><div class="md-body"><ul>' +
      "<li><code>GET /api/status</code> — health, version, uptime</li>" +
      "<li><code>GET /api/models</code> — model aliases + key status</li>" +
      "<li><code>GET /api/runs</code> · <code>GET /api/runs/{id}</code> · <code>GET /api/runs/{id}/events</code> — run history + replay</li>" +
      "<li><code>POST /api/chat</code> — start a real agent run, returns <code>run_id</code></li>" +
      "<li><code>POST /api/runs/{id}/approve</code> — resolve a pending approval</li>" +
      "<li><code>GET /api/events?run_id=…</code> — SSE stream of run events (live activity feed without run_id)</li>" +
      "<li><code>GET /api/memory</code> · <code>GET /api/memory/{name}</code> — browse memory files</li>" +
      "<li><code>GET /api/scheduler/jobs</code> — cron jobs from the SQLite store</li>" +
      "<li><code>POST /api/scheduler/jobs</code> · <code>DELETE /api/scheduler/jobs/{name}</code> · <code>POST /api/scheduler/jobs/{name}/run</code> — job management + manual trigger</li>" +
      "</ul><p class='muted'>Full contract: <code>src/agentkai/dashboard/API.md</code></p></div></div>";
    api("/api/status").then(function (s) {
      document.getElementById("set-kv").innerHTML =
        "<dt>version</dt><dd>" + esc(s.version) + "</dd>" +
        "<dt>uptime</dt><dd>" + esc(s.uptime_s) + "s</dd>" +
        "<dt>default model</dt><dd>" + esc(s.model_default) + "</dd>" +
        "<dt>active runs</dt><dd>" + esc(s.runs_active) + "</dd>";
      document.getElementById("model-pill").textContent = shortModel(s.model_default);
    }).catch(function (e) { view.innerHTML = errHtml(e); });
    api("/api/models").then(function (m) {
      var keys = Object.keys(m.key_status).map(function (p) {
        var set = m.key_status[p] === "set";
        return "<tr><td class='mono'>" + esc(p) + "</td><td>" +
          (set ? '<span class="status-pill done">set</span>'
               : '<span class="status-pill error">missing</span>') + "</td></tr>";
      }).join("");
      var models = m.models.map(function (x) {
        return "<tr><td class='mono'>" + esc(x.alias) +
          (x.alias === m.default ? ' <span class="muted">(default)</span>' : "") +
          "</td><td class='mono muted'>" + esc(x.resolved) + "</td></tr>";
      }).join("");
      document.getElementById("model-keys").innerHTML =
        '<table class="tbl"><thead><tr><th>Provider</th><th>Key</th></tr></thead><tbody>' +
        keys + "</tbody></table>" +
        '<table class="tbl" style="margin-top:12px"><thead><tr><th>Alias</th><th>Resolves to</th></tr></thead><tbody>' +
        models + "</tbody></table>";
    }).catch(function () {
      document.getElementById("model-keys").innerHTML =
        '<p class="muted">Could not load model info.</p>';
    });
  }

  function errHtml(e) {
    return '<div class="empty"><div class="big">Something went wrong</div><div class="muted mono">' +
      esc(e.message) + "</div></div>";
  }

  /* boot */
  api("/api/status").then(function (s) {
    document.getElementById("model-pill").textContent = shortModel(s.model_default);
  }).catch(function () {
    document.getElementById("conn-dot").className = "dot";
    document.getElementById("conn-text").textContent = "unreachable";
  });
  route();
})();
