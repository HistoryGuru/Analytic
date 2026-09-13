(function () {
  "use strict";

  var API_BASE = window.LD_SCOUT_API_BASE || "http://localhost:8000";

  var el = {
    searchHero: document.getElementById("search-hero"),
    searchForm: document.getElementById("search-form"),
    searchName: document.getElementById("search-name"),
    searchSchool: document.getElementById("search-school"),
    searchSubmit: document.getElementById("search-submit"),

    reportBar: document.getElementById("report-search-bar"),
    reportBarName: document.getElementById("report-search-name"),
    reportBarSchool: document.getElementById("report-search-school"),

    loading: document.getElementById("status-loading"),
    loadingText: document.getElementById("status-loading-text"),
    error: document.getElementById("status-error"),
    errorMessage: document.getElementById("status-error-message"),
    report: document.getElementById("report"),

    rName: document.getElementById("r-name"),
    rSchool: document.getElementById("r-school"),
    rMetaDot: document.getElementById("r-meta-dot"),
    rEvent: document.getElementById("r-event"),
    rRecord: document.getElementById("r-record"),
    rWinpct: document.getElementById("r-winpct"),
    rPrelimWrap: document.getElementById("r-prelim-wrap"),
    rPrelim: document.getElementById("r-prelim"),
    rElimWrap: document.getElementById("r-elim-wrap"),
    rElim: document.getElementById("r-elim"),

    rMostRead: document.getElementById("r-most-read"),
    rVsFaced: document.getElementById("r-vs-faced"),

    rNotesSection: document.getElementById("r-notes-section"),
    rNotesSummary: document.getElementById("r-notes-summary"),
    rNotesStrategy: document.getElementById("r-notes-strategy"),
    rNotesModel: document.getElementById("r-notes-model"),

    rWarningsSection: document.getElementById("r-warnings-section"),
    rWarnings: document.getElementById("r-warnings"),

    rRoundCount: document.getElementById("r-round-count"),
    rRoundTableBody: document.getElementById("r-round-table-body"),
  };

  var LOADING_MESSAGES = [
    "Searching Opencaselist for disclosed arguments…",
    "Scraping Tabroom for round results…",
    "Drafting scouting notes…",
  ];

  function setView(view) {
    // view: "hero" | "loading" | "error" | "report"
    el.searchHero.style.display = view === "hero" ? "" : "none";
    el.loading.classList.toggle("is-visible", view === "loading");
    el.error.classList.toggle("is-visible", view === "error");
    el.report.classList.toggle("is-visible", view === "report");
    el.reportBar.classList.toggle("is-visible", view !== "hero");
  }

  function cycleLoadingMessages() {
    var i = 0;
    el.loadingText.textContent = LOADING_MESSAGES[0];
    return setInterval(function () {
      i = (i + 1) % LOADING_MESSAGES.length;
      el.loadingText.textContent = LOADING_MESSAGES[i];
    }, 1800);
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function renderArgumentList(container, items, opts) {
    container.innerHTML = "";
    if (!items || items.length === 0) {
      var note = document.createElement("p");
      note.className = "empty-note";
      note.textContent = opts.emptyText;
      container.appendChild(note);
      return;
    }

    items.slice(0, opts.limit || 8).forEach(function (item) {
      var row = document.createElement("div");
      row.className = "arg-row";

      var name = document.createElement("div");
      name.className = "arg-row__name";
      name.textContent = item.argument;
      row.appendChild(name);

      var count = document.createElement("div");
      count.className = "arg-row__count mono";
      count.textContent = opts.countLabel(item);
      row.appendChild(count);

      var track = document.createElement("div");
      track.className = "arg-row__bar-track";
      var fill = document.createElement("div");
      var pct = item.win_pct == null ? 0 : item.win_pct;
      fill.className = "arg-row__bar-fill" + (pct < 50 ? " is-low" : "");
      fill.style.width = pct + "%";
      track.appendChild(fill);
      row.appendChild(track);

      var pctLabel = document.createElement("div");
      pctLabel.className = "arg-row__pct mono";
      pctLabel.textContent = item.win_pct == null ? "—" : item.win_pct + "%";
      row.appendChild(pctLabel);

      container.appendChild(row);
    });
  }

  function renderReport(data) {
    var debater = data.debater;

    el.rName.textContent = debater.matched_name || debater.query;
    el.rSchool.textContent = debater.school || "";
    el.rSchool.style.display = debater.school ? "" : "none";
    el.rMetaDot.style.display = debater.school ? "" : "none";
    el.rEvent.textContent = debater.event || "Lincoln-Douglas";

    el.rRecord.textContent = debater.overall_record || "—";
    el.rWinpct.textContent = debater.win_pct != null ? debater.win_pct + "%" : "—";

    if (debater.prelim_record) {
      el.rPrelimWrap.style.display = "";
      el.rPrelim.textContent = debater.prelim_record;
    } else {
      el.rPrelimWrap.style.display = "none";
    }
    if (debater.elim_record) {
      el.rElimWrap.style.display = "";
      el.rElim.textContent = debater.elim_record;
    } else {
      el.rElimWrap.style.display = "none";
    }

    renderArgumentList(el.rMostRead, debater.most_read_arguments, {
      limit: 8,
      emptyText: "No disclosed rounds found for this debater on Opencaselist yet.",
      countLabel: function (item) {
        return item.times_read + "×";
      },
    });

    renderArgumentList(el.rVsFaced, debater.win_pct_vs_argument_faced, {
      limit: 8,
      emptyText: "Not enough opponent disclosure data to compute this yet.",
      countLabel: function (item) {
        return item.times_faced + "×";
      },
    });

    if (data.notes && (data.notes.summary || data.notes.suggested_strategy)) {
      el.rNotesSection.style.display = "";
      el.rNotesSummary.textContent = data.notes.summary || "";
      el.rNotesStrategy.textContent = data.notes.suggested_strategy || "";
      el.rNotesModel.textContent = data.notes.model_used || "";
    } else {
      el.rNotesSection.style.display = "none";
    }

    if (debater.warnings && debater.warnings.length) {
      el.rWarningsSection.style.display = "";
      el.rWarnings.innerHTML = "";
      debater.warnings.forEach(function (w) {
        var div = document.createElement("div");
        div.className = "warnings__item";
        div.textContent = w;
        el.rWarnings.appendChild(div);
      });
    } else {
      el.rWarningsSection.style.display = "none";
    }

    var rounds = debater.rounds || [];
    el.rRoundCount.textContent = rounds.length;
    el.rRoundTableBody.innerHTML = "";
    rounds.forEach(function (r) {
      var tr = document.createElement("tr");

      var resultClass = "result-tag--unknown";
      var resultText = r.result || "?";
      if (r.result === "Win") resultClass = "result-tag--win";
      if (r.result === "Loss") resultClass = "result-tag--loss";

      tr.innerHTML =
        "<td>" + escapeHtml(r.tournament || "—") + "</td>" +
        "<td>" + escapeHtml(r.round_name || "—") + "</td>" +
        "<td>" + escapeHtml(r.side || "—") + "</td>" +
        "<td>" + escapeHtml(r.opponent || "—") + "</td>" +
        "<td>" + escapeHtml(r.argument || "—") + "</td>" +
        "<td><span class=\"result-tag " + resultClass + "\">" + escapeHtml(resultText) + "</span></td>";
      el.rRoundTableBody.appendChild(tr);
    });
  }

  function runSearch(name, school) {
    name = (name || "").trim();
    school = (school || "").trim();
    if (!name || !school) return;

    // keep both search bars in sync
    el.searchName.value = name;
    el.searchSchool.value = school;
    el.reportBarName.value = name;
    el.reportBarSchool.value = school;

    setView("loading");
    var loadingTimer = cycleLoadingMessages();
    el.searchSubmit.disabled = true;

    var url = new URL(API_BASE + "/api/debater");
    url.searchParams.set("q", name);
    if (school) url.searchParams.set("school", school);

    fetch(url.toString())
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () {
            return { detail: "The API returned an unexpected error (status " + resp.status + ")." };
          }).then(function (body) {
            throw new Error(body.detail || "Something went wrong looking that debater up.");
          });
        }
        return resp.json();
      })
      .then(function (data) {
        clearInterval(loadingTimer);
        el.searchSubmit.disabled = false;
        renderReport(data);
        setView("report");
      })
      .catch(function (err) {
        clearInterval(loadingTimer);
        el.searchSubmit.disabled = false;
        el.errorMessage.textContent =
          err.message ||
          "Couldn't reach the LD Scout API. If you're running this locally, make sure the backend is running and config.js points at it.";
        setView("error");
      });
  }

  el.searchForm.addEventListener("submit", function (e) {
    e.preventDefault();
    runSearch(el.searchName.value, el.searchSchool.value);
  });

  el.reportBar.addEventListener("submit", function (e) {
    e.preventDefault();
    runSearch(el.reportBarName.value, el.reportBarSchool.value);
  });

  setView("hero");
})();
