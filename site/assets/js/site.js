/* agentkai website :  site.js (vanilla, no dependencies)
   Progressive enhancement only: all content is readable with JS off. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- nav ---------- */
  var nav = document.getElementById("siteNav");
  var toggle = document.getElementById("navToggle");
  var links = document.getElementById("navLinks");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    links.addEventListener("click", function (e) {
      if (e.target.tagName === "A") {
        links.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }
  function onScroll() {
    if (nav) nav.classList.toggle("scrolled", window.scrollY > 8);
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ---------- copy buttons: [data-copy] ---------- */
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      function done() {
        btn.classList.add("copied");
        var label = btn.querySelector(".copy-label");
        var original = btn.innerHTML;
        if (btn.classList.contains("copy-btn")) btn.textContent = "Copied ✓";
        setTimeout(function () {
          btn.classList.remove("copied");
          if (btn.classList.contains("copy-btn")) btn.innerHTML = original;
        }, 1500);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) { /* noop */ }
        document.body.removeChild(ta);
        done();
      }
    });
  });

  /* ---------- scroll reveal ---------- */
  var revealEls = document.querySelectorAll(".reveal");
  if (reducedMotion || !("IntersectionObserver" in window)) {
    revealEls.forEach(function (el) { el.classList.add("in"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    revealEls.forEach(function (el) { io.observe(el); });
    /* safety net: nothing stays hidden if the observer never fires */
    setTimeout(function () {
      revealEls.forEach(function (el) { el.classList.add("in"); });
    }, 4000);
  }

  /* ---------- CLI tabs (examples verified against `agentkai --help`) ---------- */
  var SNIPPETS = {
    run: '<span class="c"># run one agent task, streaming</span>\n' +
      '<span class="a">agentkai</span> run "list the files in my home directory and summarize what this machine is for"\n' +
      '<span class="c"># → the answer streams as the model thinks; medium/high-risk tools ask first</span>\n' +
      '<span class="c"># pass -y to auto-approve, only when you trust the task</span>',
    chat: '<span class="c"># one-shot answer, no loop UI</span>\n' +
      '<span class="a">agentkai</span> chat "what day is it?" --model gemini\n' +
      '<span class="g">Thursday.</span>\n' +
      '<span class="c"># pick the model per call: --model claude|gemini|gpt|glm|local</span>',
    models: '<span class="c"># aliases + which keys are set (keys themselves are never shown)</span>\n' +
      '<span class="a">agentkai</span> models\n' +
      '<span class="g">claude   → anthropic/claude-sonnet-4-5   ✓ key set</span>\n' +
      '<span class="g">gemini   → gemini/gemini-2.5-flash        ✓ key set</span>\n' +
      '<span class="g">gpt      → openai/gpt-5                    ✗ missing</span>\n' +
      '<span class="g">local    → ollama/qwen3:32b               ✓ local</span>',
    dashboard: '<span class="c"># local web dashboard: chat, run timelines, memory, scheduler</span>\n' +
      '<span class="a">agentkai</span> dashboard\n' +
      '<span class="g">Dashboard: http://127.0.0.1:8931/?token=…</span>\n' +
      '<span class="c"># binds 127.0.0.1 only (never LAN/internet), fresh token per launch</span>',
    scheduler: '<span class="c"># background jobs: cron, one-shot, heartbeat, nightly dreaming</span>\n' +
      '<span class="a">agentkai</span> scheduler add morning-brief --schedule "0 7 * * *" \\\n' +
      '  --prompt "Summarize today\'s calendar and unread email."\n' +
      '<span class="a">agentkai</span> scheduler list\n' +
      '<span class="a">agentkai</span> scheduler run-once morning-brief   <span class="c"># run now</span>\n' +
      '<span class="a">agentkai</span> scheduler remove morning-brief',
    skills: '<span class="c"># bundled skills: github gmail google_calendar health image_search</span>\n' +
      '<span class="c"># media payments places shopping spotify travel voice</span>\n' +
      '<span class="a">agentkai</span> skills list\n' +
      '<span class="g">github ✓   gmail ✓   google_calendar ✓   health ✓   …</span>\n' +
      '<span class="c"># installs are hash-verified before any skill code runs</span>'
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

  /* ---------- FAQ accordion ---------- */
  document.querySelectorAll(".faq-item").forEach(function (item) {
    var q = item.querySelector(".faq-q");
    var a = item.querySelector(".faq-a");
    if (!q || !a) return;
    q.addEventListener("click", function () {
      var open = item.classList.toggle("open");
      q.setAttribute("aria-expanded", open ? "true" : "false");
      a.style.maxHeight = open ? a.scrollHeight + "px" : "0px";
    });
  });
})();
