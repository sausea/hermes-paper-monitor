const papers = Array.isArray(window.__HERMES_PAPERS__) ? window.__HERMES_PAPERS__ : [];
const stats = window.__HERMES_STATS__ || {};
const READ_STORAGE_KEY = "hermes.paper.read.v1";

const state = {
  query: "",
  source: "all",
  status: "all",
  read: "all",
  keyword: "all",
};

const elements = {
  heroStats: document.getElementById("hero-stats"),
  searchInput: document.getElementById("search-input"),
  sourceSelect: document.getElementById("source-select"),
  statusSelect: document.getElementById("status-select"),
  readSelect: document.getElementById("read-select"),
  keywordSelect: document.getElementById("keyword-select"),
  resultsSummary: document.getElementById("results-summary"),
  paperGrid: document.getElementById("paper-grid"),
};

const readState = loadReadState();

function loadReadState() {
  try {
    const raw = window.localStorage.getItem(READ_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    return parsed;
  } catch (error) {
    console.warn("Failed to load read state", error);
    return {};
  }
}

function persistReadState() {
  try {
    window.localStorage.setItem(READ_STORAGE_KEY, JSON.stringify(readState));
  } catch (error) {
    console.warn("Failed to persist read state", error);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function containsCjkText(value) {
  return /[\u4e00-\u9fff]/.test(value || "");
}

function isExternalHref(value) {
  return /^https?:\/\//i.test(value || "");
}

function linkTargetFor(href) {
  return "_blank";
}

function linkRelFor(href) {
  return isExternalHref(href) ? "noreferrer" : "noopener noreferrer";
}

function renderLink(href, label) {
  if (!href) {
    return "";
  }
  const safeHref = String(href);
  return `<a href="${escapeHtml(safeHref)}" target="${linkTargetFor(safeHref)}" rel="${linkRelFor(safeHref)}" data-link-href="${escapeHtml(safeHref)}">${escapeHtml(label)}</a>`;
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
}

function formatDate(value) {
  if (!value) {
    return "未知日期";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(date);
}

function badgeList(items, className = "") {
  if (!items || items.length === 0) {
    return "";
  }
  return items
    .map((item) => `<span class="badge ${className}">${escapeHtml(item)}</span>`)
    .join("");
}

function paperKey(paper) {
  return paper.paper_id || `${paper.source_id || "paper"}:${paper.title || ""}`;
}

function isPaperRead(paper) {
  return Boolean(readState[paperKey(paper)]);
}

function markPaperRead(paperId, read) {
  if (!paperId) {
    return;
  }
  if (read) {
    readState[paperId] = new Date().toISOString();
  } else {
    delete readState[paperId];
  }
  persistReadState();
}

function readCounts(items = papers) {
  const read = items.filter((paper) => isPaperRead(paper)).length;
  return {
    read,
    unread: Math.max(items.length - read, 0),
  };
}

function renderHero() {
  const counts = readCounts();
  const cards = [
    ["论文总数", stats.paper_count ?? papers.length],
    ["未读", counts.unread],
    ["已完成", stats.ready_count ?? papers.filter((paper) => paper.status === "ready").length],
    ["来源数", stats.source_count ?? uniqueSorted(papers.map((paper) => paper.source_label)).length],
    ["最近更新", formatDate(stats.updated_at || "")],
  ];
  elements.heroStats.innerHTML = cards
    .map(
      ([label, value]) => `
        <div class="stat-card">
          <span class="stat-label">${escapeHtml(label)}</span>
          <strong class="stat-value">${escapeHtml(value)}</strong>
        </div>
      `
    )
    .join("");
}

function populateFilters() {
  const sources = uniqueSorted(papers.map((paper) => paper.source_label));
  const keywords = uniqueSorted(
    papers.flatMap((paper) => Array.isArray(paper.keywords) ? paper.keywords : [])
  );

  for (const source of sources) {
    const option = document.createElement("option");
    option.value = source;
    option.textContent = source;
    elements.sourceSelect.append(option);
  }

  for (const keyword of keywords) {
    const option = document.createElement("option");
    option.value = keyword;
    option.textContent = keyword;
    elements.keywordSelect.append(option);
  }
}

function filteredPapers() {
  const query = state.query.trim().toLowerCase();
  return papers
    .filter((paper) => {
      if (state.source !== "all" && paper.source_label !== state.source) {
        return false;
      }
      if (state.status !== "all" && paper.status !== state.status) {
        return false;
      }
      if (state.read === "read" && !isPaperRead(paper)) {
        return false;
      }
      if (state.read === "unread" && isPaperRead(paper)) {
        return false;
      }
      if (state.keyword !== "all") {
        const keywords = Array.isArray(paper.keywords) ? paper.keywords : [];
        if (!keywords.includes(state.keyword)) {
          return false;
        }
      }
      if (!query) {
        return true;
      }
      const haystack = [
        paper.title,
        paper.zh_title,
        paper.abstract,
        paper.zh_summary,
        paper.source_label,
        ...(paper.keywords || []),
        ...(paper.categories || []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    })
    .sort((left, right) => {
      const leftDate = new Date(left.published_at || left.discovered_at || 0).getTime();
      const rightDate = new Date(right.published_at || right.discovered_at || 0).getTime();
      return rightDate - leftDate;
    });
}

function renderSummary(items) {
  const ready = items.filter((paper) => paper.status === "ready").length;
  const pending = items.filter((paper) => paper.status === "pending_enrichment").length;
  const counts = readCounts(items);
  elements.resultsSummary.innerHTML = `
    <div class="summary-chip">当前结果 <strong>${items.length}</strong></div>
    <div class="summary-chip">未读 <strong>${counts.unread}</strong></div>
    <div class="summary-chip">已读 <strong>${counts.read}</strong></div>
    <div class="summary-chip">已完成 <strong>${ready}</strong></div>
    <div class="summary-chip">待补全 <strong>${pending}</strong></div>
    <div class="summary-chip">排序 <strong>最新优先</strong></div>
  `;
}

function renderPapers() {
  const items = filteredPapers();
  renderSummary(items);
  if (items.length === 0) {
    elements.paperGrid.innerHTML = `
      <article class="empty-state">
        <h2>没有匹配结果</h2>
        <p>可以调整关键词、来源或状态筛选。</p>
      </article>
    `;
    return;
  }

  elements.paperGrid.innerHTML = items
    .map((paper) => {
      const read = isPaperRead(paper);
      const authors = Array.isArray(paper.authors) ? paper.authors.join(", ") : "";
      const affiliations = Array.isArray(paper.affiliations) ? paper.affiliations.join(" / ") : "";
      const zhTitle = (paper.zh_title || "") !== (paper.title || "") ? paper.zh_title || "" : "";
      const abstract = paper.abstract || "";
      const hasCjkAbstract = containsCjkText(abstract);
      const summary = paper.zh_summary || (hasCjkAbstract ? abstract : "") || "尚未生成中文摘要";
      const abstractLabel = hasCjkAbstract ? "原摘要" : "英文摘要";
      const abstractText = abstract || (hasCjkAbstract ? "暂无原摘要" : "暂无英文摘要");
      const links = [
        renderLink(paper.pdf_href, "PDF"),
        renderLink(paper.landing_url, "原文页"),
        paper.doi ? renderLink(`https://doi.org/${paper.doi}`, "DOI") : "",
      ]
        .filter(Boolean)
        .join("");

      return `
        <article class="paper-card ${read ? "is-read" : "is-unread"}">
          <div class="paper-topline">
            <div class="paper-pills">
              <span class="status-pill status-${escapeHtml(paper.status || "unknown")}">${escapeHtml(paper.status || "unknown")}</span>
              <span class="read-pill ${read ? "read-read" : "read-unread"}">${read ? "已读" : "未读"}</span>
            </div>
            <span class="paper-date">${escapeHtml(formatDate(paper.published_at || paper.discovered_at))}</span>
          </div>
          <h2>${escapeHtml(paper.title || "Untitled")}</h2>
          ${zhTitle ? `<p class="paper-title-zh">${escapeHtml(zhTitle)}</p>` : ""}
          <p class="paper-source">${escapeHtml(paper.source_label || "")} · ${escapeHtml(paper.collection || paper.publisher || "")}</p>
          <p class="paper-summary">${escapeHtml(summary)}</p>
          <details class="paper-details">
            <summary>展开详情</summary>
            <p><strong>${escapeHtml(abstractLabel)}：</strong>${escapeHtml(abstractText)}</p>
            <p><strong>作者：</strong>${escapeHtml(authors || "未知")}</p>
            <p><strong>单位：</strong>${escapeHtml(affiliations || "待补全")}</p>
            <p><strong>分类：</strong>${badgeList(paper.categories || [], "badge-soft")}</p>
            <p><strong>关键词：</strong>${badgeList(paper.keywords || [], "badge-accent")}</p>
            ${paper.notes ? `<p><strong>备注：</strong>${escapeHtml(paper.notes)}</p>` : ""}
          </details>
          <div class="paper-links">
            <button class="read-toggle" type="button" data-paper-id="${escapeHtml(paperKey(paper))}" data-read-action="${read ? "unread" : "read"}">
              ${read ? "标记未读" : "标记已读"}
            </button>
            ${links || "<span class=\"muted\">暂无可用链接</span>"}
          </div>
        </article>
      `;
    })
    .join("");
}

elements.searchInput.addEventListener("input", (event) => {
  state.query = event.target.value;
  renderPapers();
});

elements.sourceSelect.addEventListener("change", (event) => {
  state.source = event.target.value;
  renderPapers();
});

elements.statusSelect.addEventListener("change", (event) => {
  state.status = event.target.value;
  renderPapers();
});

elements.readSelect.addEventListener("change", (event) => {
  state.read = event.target.value;
  renderPapers();
});

elements.keywordSelect.addEventListener("change", (event) => {
  state.keyword = event.target.value;
  renderPapers();
});

elements.paperGrid.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-link-href]");
  if (link) {
    const href = link.getAttribute("href") || link.dataset.linkHref || "";
    if (isExternalHref(href)) {
      event.preventDefault();
      const opened = window.open(href, "_blank", "noopener,noreferrer");
      if (!opened) {
        window.location.assign(href);
      }
    }
    return;
  }
  const button = event.target.closest("[data-paper-id]");
  if (!button) {
    return;
  }
  const paperId = button.getAttribute("data-paper-id") || "";
  const action = button.getAttribute("data-read-action") || "read";
  markPaperRead(paperId, action === "read");
  renderHero();
  renderPapers();
});

renderHero();
populateFilters();
renderPapers();
