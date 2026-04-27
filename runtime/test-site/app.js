const papers = Array.isArray(window.__HERMES_PAPERS__) ? window.__HERMES_PAPERS__ : [];
const stats = window.__HERMES_STATS__ || {};

const state = {
  query: "",
  source: "all",
  status: "all",
  keyword: "all",
};

const elements = {
  heroStats: document.getElementById("hero-stats"),
  searchInput: document.getElementById("search-input"),
  sourceSelect: document.getElementById("source-select"),
  statusSelect: document.getElementById("status-select"),
  keywordSelect: document.getElementById("keyword-select"),
  resultsSummary: document.getElementById("results-summary"),
  paperGrid: document.getElementById("paper-grid"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
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

function renderHero() {
  const cards = [
    ["论文总数", stats.paper_count ?? papers.length],
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
  elements.resultsSummary.innerHTML = `
    <div class="summary-chip">当前结果 <strong>${items.length}</strong></div>
    <div class="summary-chip">已完成 <strong>${ready}</strong></div>
    <div class="summary-chip">待补全 <strong>${pending}</strong></div>
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
      const authors = Array.isArray(paper.authors) ? paper.authors.join(", ") : "";
      const affiliations = Array.isArray(paper.affiliations) ? paper.affiliations.join(" / ") : "";
      const summary = paper.zh_summary || "尚未生成中文摘要";
      const abstract = paper.abstract || "暂无英文摘要";
      const links = [
        paper.pdf_href ? `<a href="${escapeHtml(paper.pdf_href)}" target="_blank" rel="noreferrer">PDF</a>` : "",
        paper.landing_url ? `<a href="${escapeHtml(paper.landing_url)}" target="_blank" rel="noreferrer">原文页</a>` : "",
        paper.doi ? `<a href="https://doi.org/${escapeHtml(paper.doi)}" target="_blank" rel="noreferrer">DOI</a>` : "",
      ]
        .filter(Boolean)
        .join("");

      return `
        <article class="paper-card">
          <div class="paper-topline">
            <span class="status-pill status-${escapeHtml(paper.status || "unknown")}">${escapeHtml(paper.status || "unknown")}</span>
            <span class="paper-date">${escapeHtml(formatDate(paper.published_at || paper.discovered_at))}</span>
          </div>
          <h2>${escapeHtml(paper.title || "Untitled")}</h2>
          <p class="paper-source">${escapeHtml(paper.source_label || "")} · ${escapeHtml(paper.collection || paper.publisher || "")}</p>
          <p class="paper-summary">${escapeHtml(summary)}</p>
          <details class="paper-details">
            <summary>展开详情</summary>
            <p><strong>英文摘要：</strong>${escapeHtml(abstract)}</p>
            <p><strong>作者：</strong>${escapeHtml(authors || "未知")}</p>
            <p><strong>单位：</strong>${escapeHtml(affiliations || "待补全")}</p>
            <p><strong>分类：</strong>${badgeList(paper.categories || [], "badge-soft")}</p>
            <p><strong>关键词：</strong>${badgeList(paper.keywords || [], "badge-accent")}</p>
            ${paper.notes ? `<p><strong>备注：</strong>${escapeHtml(paper.notes)}</p>` : ""}
          </details>
          <div class="paper-links">${links || "<span class=\"muted\">暂无可用链接</span>"}</div>
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

elements.keywordSelect.addEventListener("change", (event) => {
  state.keyword = event.target.value;
  renderPapers();
});

renderHero();
populateFilters();
renderPapers();
