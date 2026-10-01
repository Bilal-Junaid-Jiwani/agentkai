/* agentkai website — site.js (vanilla, no dependencies) */
(function () {
  "use strict";

  // nav toggle (mobile)
  var toggle = document.getElementById("navToggle");
  var links = document.getElementById("navLinks");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // copy buttons: [data-copy]
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      function done() {
        var old = btn.textContent;
        btn.textContent = "Copied";
        setTimeout(function () { btn.textContent = old; }, 1400);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta);
        done();
      }
    });
  });

  // CLI tab examples (real commands, verified against agentkai --help)
  var SNIPPETS = {
    run: '<span class="c"># run one agent task, streaming</span>\n' +
      '<span class="a">agentkai</span> run "list the files in my home directory and summarize what this machine is for"\n' +
      '<span class="c"># → the answer streams as the model thinks; medium/high-risk tools ask first</span>\n' +
      '<span class="c"># pass -y to auto-approve, only when you trust the task</span>',
    chat: '<span class="c"># one-shot answer, no loop UI</span>\n' +
      '<span class="a">agentkai</span> chat "what day is it?" --model gemini\n' +
      '<span class="g">Thursday.</span>\n' +
      '<span class="c"># pick the model per call: --model claude|gemini|gpt|glm|local</span>',
    dashboard: '<span class="c"># local web dashboard: chat, run timelines, memory, scheduler</span>\n' +
      '<span class="a">agentkai</span> dashboard\n' +
      '<span class="g">Dashboard: http://127.0.0.1:8931/?token=…</span>\n' +
      '<span class="c"># binds 127.0.0.1 only (never LAN/internet), per-launch token</span>',
    scheduler: '<span class="c"># background jobs: cron, one-shot, heartbeat, dreaming</span>\n' +
      '<span class="a">agentkai</span> scheduler add morning-brief --schedule "0 7 * * *" \\\n' +
      '  --prompt "Summarize today\'s calendar and unread email."\n' +
      '<span class="a">agentkai</span> scheduler list\n' +
      '<span class="a">agentkai</span> scheduler run-once morning-brief   <span class="c"># run now</span>\n' +
      '<span class="a">agentkai</span> scheduler remove morning-brief',
    skills: '<span class="c"># bundled skills: github gmail google_calendar health image_search</span>\n' +
      '<span class="c"># media payments places shopping spotify travel voice</span>\n' +
      '<span class="a">agentkai</span> skills list\n' +
      '<span class="g">github ✓   gmail ✓   google_calendar ✓   health ✓   …</span>\n' +
      '<span class="c"># your own: a SKILL.md + optional tools.py in ~/.agentkai/skills/</span>'
  };
  var pre = document.getElementById("cliPre");
  document.querySelectorAll(".cli-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".cli-tab").forEach(function (t) {
        t.setAttribute("aria-selected", "false");
      });
      tab.setAttribute("aria-selected", "true");
      if (pre) pre.innerHTML = SNIPPETS[tab.getAttribute("data-cli")] || "";
    });
  });
})();
