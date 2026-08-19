/* VaultDrive home enhance — feed / badges / drawer / search / PWA */
(function () {
  const HIST_KEY = 'vd_search_hist_v1';
  const LOCAL_FAV_KEY = 'vd_local_favs_v1';
  let acTimer = null;
  let acItems = [];
  let acIndex = -1;
  let globalAcTimer = null;
  let globalAcItems = [];
  let globalAcIndex = -1;
  let deferredInstall = null;
  let latestOffset = 0;
  let latestTotal = 0;
  let latestItems = [];

  function esc(s) {
    if (window.esc) return window.esc(s);
    const d = document.createElement('div');
    d.textContent = String(s == null ? '' : s);
    return d.innerHTML;
  }

  function hashHue(str) {
    let h = 0;
    const s = String(str || '');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h % 360;
  }

  function typeIcon(type, mediaType) {
    const t = String(type || mediaType || '').toLowerCase();
    if (t.includes('剧') || t === 'tv') return '📺';
    if (t.includes('动') || t === 'anime') return '✨';
    if (t.includes('综')) return '🎤';
    return '🎬';
  }

  function firstGlyph(title) {
    const t = String(title || '').trim();
    if (!t) return '·';
    const m = t.match(/[\u4e00-\u9fffA-Za-z0-9]/);
    return m ? m[0] : t[0];
  }

  window.vdPosterFail = function (img) {
    if (!img) return;
    img.style.display = 'none';
    const fb = img.nextElementSibling;
    if (fb && fb.classList.contains('poster-fallback')) fb.hidden = false;
  };

  window.vdPosterRetry = function (btn) {
    const fb = btn && btn.closest('.poster-fallback');
    const wrap = fb && fb.parentElement;
    const img = wrap && wrap.querySelector('img');
    const src = (img && (img.getAttribute('data-src') || img.src)) || '';
    if (!img || !src) return;
    fb.hidden = true;
    img.style.display = '';
    const base = src.split('?')[0];
    img.src = base + '?retry=' + Date.now();
  };

  function posterFallbackHtml(title, type, mediaType, visible) {
    const hue = hashHue(title);
    const ch = firstGlyph(title);
    const ico = typeIcon(type, mediaType);
    return `
      <div class="poster-fallback"${visible ? '' : ' hidden'}>
        <div class="pf-bg" style="--pf-hue:${hue}"></div>
        <span class="pf-type" aria-hidden="true">${ico}</span>
        <span class="pf-glyph">${esc(ch)}</span>
        <button type="button" class="pf-retry" onclick="event.stopPropagation();vdPosterRetry(this)" title="重试加载海报">↻</button>
      </div>`;
  }

  function posterThumbHtml(opts) {
    const title = opts.title || '';
    const poster = opts.poster || '';
    const type = opts.type || '';
    const mediaType = opts.mediaType || '';
    if (!poster) return posterFallbackHtml(title, type, mediaType, true);
    return `
      <img src="${esc(poster)}" data-src="${esc(poster)}" alt="${esc(title)}"
           loading="lazy" decoding="async" onerror="vdPosterFail(this)">
      ${posterFallbackHtml(title, type, mediaType, false)}
    `;
  }

  function getHist() {
    try { return JSON.parse(localStorage.getItem(HIST_KEY) || '[]'); }
    catch (e) { return []; }
  }
  function pushHist(q) {
    q = (q || '').trim();
    if (!q) return;
    const list = getHist().filter(x => x !== q);
    list.unshift(q);
    localStorage.setItem(HIST_KEY, JSON.stringify(list.slice(0, 8)));
    renderHist();
  }

  function ensureHomeSections() {
    const home = document.getElementById('page-home');
    if (!home || document.getElementById('homePersonal')) return;

    const hero = home.querySelector('.archive-hero-content');
    if (hero && !document.getElementById('homeSearchTools')) {
      const tools = document.createElement('div');
      tools.className = 'home-search-tools';
      tools.id = 'homeSearchTools';
      tools.innerHTML = `
        <div class="home-hist-row" id="homeHistRow"></div>
      `;
      hero.appendChild(tools);

      const box = hero.querySelector('.home-search-box');
      if (box && !document.getElementById('homeAcPanel')) {
        const panel = document.createElement('div');
        panel.className = 'home-ac-panel';
        panel.id = 'homeAcPanel';
        box.appendChild(panel);
      }
    }

    const wallHead = home.querySelector('.poster-wall-head');
    if (!wallHead) return;

    const personal = document.createElement('div');
    personal.className = 'home-personal hidden';
    personal.id = 'homePersonal';
    personal.innerHTML = `
      <div class="home-rail-head" style="margin-bottom:10px">
        <div>
          <h2>为你准备</h2>
          <p>订阅新货 · 想看有源 · 收藏</p>
        </div>
      </div>
      <div class="home-personal-grid" id="homePersonalGrid"></div>
    `;

    const feed = document.createElement('div');
    feed.id = 'homeLocalFeed';
    feed.innerHTML = `
      <section class="home-rail" id="homeHotRail">
        <div class="home-rail-head">
          <div><h2>热搜</h2><p>站内真实搜索热度</p></div>
        </div>
        <div class="home-hot-row" id="homeHotRow"></div>
      </section>
      <section class="home-rail" id="homeLatestRail">
        <div class="home-rail-head">
          <div><h2>最新入库</h2><p>本站刚收录的资源</p></div>
          <div class="home-rail-actions">
            <button type="button" class="home-pill" id="homeLatestMore" hidden>加载更多</button>
          </div>
        </div>
        <div class="home-latest-grid" id="homeLatestRow"></div>
      </section>
      <section class="home-rail" id="homeClickRail">
        <div class="home-rail-head">
          <div><h2>近 7 天热搜</h2><p>热搜词 × 近一周入库片源</p></div>
        </div>
        <div class="home-hscroll" id="homeClickRow"></div>
      </section>
    `;

    wallHead.parentNode.insertBefore(personal, wallHead);
    wallHead.parentNode.insertBefore(feed, wallHead);

    if (!document.getElementById('homeCatNav')) {
      const catNav = document.createElement('div');
      catNav.className = 'home-cat-nav';
      catNav.id = 'homeCatNav';
      catNav.innerHTML = `
        <button type="button" class="home-cat-btn" data-type="movie"><span class="ico">🎬</span>电影</button>
        <button type="button" class="home-cat-btn" data-type="tv"><span class="ico">📺</span>剧集</button>
        <button type="button" class="home-cat-btn" data-type="anime"><span class="ico">✨</span>动画</button>
        <button type="button" class="home-cat-btn" data-type="top_rated"><span class="ico">★</span>高分</button>
        <button type="button" class="home-cat-btn" data-act="latest"><span class="ico">🆕</span>最新入库</button>
      `;
      wallHead.parentNode.insertBefore(catNav, wallHead);
      catNav.querySelectorAll('.home-cat-btn').forEach(btn => {
        btn.onclick = () => {
          const act = btn.getAttribute('data-act');
          if (act === 'latest') {
            document.getElementById('homeLatestRail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return;
          }
          const type = btn.getAttribute('data-type');
          const bar = document.getElementById('posterFilterBar');
          if (bar && typeof window.setHomeChip === 'function') {
            const chip = bar.querySelector(`[onclick*="'${type}'"]`);
            if (chip) setHomeChip(chip, type);
          } else if (typeof window.loadPosterWall === 'function') {
            loadPosterWall(type);
          }
          document.getElementById('posterWallGrid')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        };
      });
    }

    if (!document.getElementById('siteFooter')) {
      const footer = document.createElement('footer');
      footer.className = 'site-footer';
      footer.id = 'siteFooter';
      footer.innerHTML = `
        <p>VaultDrive · 私人胶片库 · 搜索免费；收藏需登录（未登录可本地暂存）</p>
        <nav aria-label="页脚链接">
          <a href="/privacy">隐私政策</a>
          <a href="https://github.com/aiqinghaiwork163/resource_web" target="_blank" rel="noopener noreferrer">GitHub</a>
          <a href="#" onclick="event.preventDefault();openMovieRequest && openMovieRequest()">反馈 / 求片</a>
        </nav>
      `;
      const main = document.querySelector('.main-content') || document.body;
      main.appendChild(footer);
    }

    if (!document.getElementById('detailDrawer')) {
      const mask = document.createElement('div');
      mask.className = 'detail-drawer-mask';
      mask.id = 'detailDrawerMask';
      mask.onclick = closeDetailDrawer;
      const drawer = document.createElement('aside');
      drawer.className = 'detail-drawer';
      drawer.id = 'detailDrawer';
      drawer.innerHTML = `
        <div class="detail-drawer-head">
          <div>
            <h3 id="detailTitle">详情</h3>
            <div class="sub" id="detailSub"></div>
          </div>
          <button type="button" class="detail-drawer-close" aria-label="关闭" onclick="closeDetailDrawer()">✕</button>
        </div>
        <div class="detail-drawer-body" id="detailBody"></div>
      `;
      document.body.appendChild(mask);
      document.body.appendChild(drawer);
    }

    if (!document.getElementById('pwaHint')) {
      const hint = document.createElement('div');
      hint.className = 'pwa-hint';
      hint.id = 'pwaHint';
      hint.innerHTML = `
        <div style="font-size:.85rem;color:var(--text-secondary)">安装 VaultDrive 到主屏幕，当 App 用</div>
        <div style="display:flex;gap:6px">
          <button type="button" class="no" id="pwaNo">稍后</button>
          <button type="button" class="ok" id="pwaOk">安装</button>
        </div>
      `;
      document.body.appendChild(hint);
    }
    renderHist();
    bindHomeSearch();
  }


  function renderHist() {
    const el = document.getElementById('homeHistRow');
    if (!el) return;
    const list = getHist();
    if (!list.length) {
      el.innerHTML = '';
      return;
    }
    el.innerHTML = list.map(q =>
      `<button type="button" class="home-hist-chip" data-q="${esc(q)}">${esc(q)}</button>`
    ).join('');
    el.querySelectorAll('.home-hist-chip').forEach(btn => {
      btn.onclick = () => {
        const q = btn.getAttribute('data-q') || '';
        const input = document.getElementById('homeSearchInput');
        if (input) input.value = q;
        runHomeSearch(q);
      };
    });
  }

  function bindHomeSearch() {
    const input = document.getElementById('homeSearchInput');
    if (!input || input.dataset.enhanceBound === '1') return;
    input.dataset.enhanceBound = '1';
    input.addEventListener('input', () => {
      clearTimeout(acTimer);
      acTimer = setTimeout(() => loadAutocomplete(input.value.trim()), 180);
    });
    input.addEventListener('keydown', (e) => {
      const panel = document.getElementById('homeAcPanel');
      if (e.key === 'ArrowDown' && acItems.length) {
        e.preventDefault();
        acIndex = Math.min(acIndex + 1, acItems.length - 1);
        paintAc();
      } else if (e.key === 'ArrowUp' && acItems.length) {
        e.preventDefault();
        acIndex = Math.max(acIndex - 1, 0);
        paintAc();
      } else if (e.key === 'Enter') {
        if (acIndex >= 0 && acItems[acIndex]) {
          e.preventDefault();
          pickAc(acItems[acIndex].keyword);
        }
      } else if (e.key === 'Escape' && panel) {
        panel.classList.remove('open');
      }
    });
    document.addEventListener('click', (e) => {
      const panel = document.getElementById('homeAcPanel');
      const box = document.querySelector('.home-search-box');
      if (panel && box && !box.contains(e.target)) panel.classList.remove('open');
    });

    // override executeHomeSearch if present
    window.executeHomeSearch = function () {
      const q = (document.getElementById('homeSearchInput') || {}).value || '';
      runHomeSearch(q.trim());
    };
  }

  async function loadAutocomplete(q) {
    const panel = document.getElementById('homeAcPanel');
    if (!panel) return;
    if (!q) {
      panel.classList.remove('open');
      panel.innerHTML = '';
      acItems = [];
      acIndex = -1;
      return;
    }
    try {
      const r = await fetch('/api/autocomplete?q=' + encodeURIComponent(q));
      const d = await r.json();
      acItems = d.items || [];
      acIndex = -1;
      paintAc();
    } catch (e) {
      panel.classList.remove('open');
    }
  }

  function paintAc() {
    const panel = document.getElementById('homeAcPanel');
    if (!panel) return;
    if (!acItems.length) {
      panel.classList.remove('open');
      panel.innerHTML = '';
      return;
    }
    panel.innerHTML = acItems.map((it, i) =>
      `<div class="home-ac-item${i === acIndex ? ' active' : ''}" data-i="${i}">
        <span>${esc(it.keyword || '')}</span>
        <span style="color:var(--text-muted);font-size:.72rem">${it.hit_count != null ? it.hit_count : ''}</span>
      </div>`
    ).join('');
    panel.classList.add('open');
    panel.querySelectorAll('.home-ac-item').forEach(el => {
      el.onclick = () => pickAc(acItems[Number(el.getAttribute('data-i'))].keyword);
    });
  }

  function pickAc(keyword) {
    const input = document.getElementById('homeSearchInput');
    if (input) input.value = keyword;
    const panel = document.getElementById('homeAcPanel');
    if (panel) panel.classList.remove('open');
    runHomeSearch(keyword);
  }

  function runHomeSearch(q) {
    q = (q || '').trim();
    if (!q) return;
    pushHist(q);
    if (typeof window.searchResource === 'function') {
      window.searchResource(q);
    }
  }

  function skelRow(el, n) {
    if (!el) return;
    el.innerHTML = `<div class="home-skel-row">${Array.from({ length: n || 6 }, () => '<div class="home-skel"></div>').join('')}</div>`;
  }

  function skelGrid(el, n) {
    if (!el) return;
    el.innerHTML = `<div class="home-latest-grid">${Array.from({ length: n || 12 }, () => '<div class="home-skel" style="height:160px;border-radius:10px"></div>').join('')}</div>`;
  }

  function cardHtml(it) {
    const title = it.display_title || it.hot_keyword || it.title || it.keyword || '';
    const src = it.source_label || it.source || '';
    const titleJs = JSON.stringify(it.hot_keyword || title);
    const fullTitle = title.trim();
    return `
      <article class="home-chip-card" data-q="${esc(title)}" onclick='openDetailDrawer(${titleJs})'>
        <div class="thumb poster-thumb">
          ${posterThumbHtml({ title, poster: it.poster || '', type: it.type, mediaType: it.media_type })}
        </div>
        <div class="meta">
          <div class="t" title="${esc(fullTitle)}">${esc(title)}</div>
          <div class="s">${esc([src, it.year].filter(Boolean).join(' · '))}</div>
        </div>
      </article>
    `;
  }

  async function fillMissingPosters(items, rowEl) {
    if (!rowEl || !items || !items.length) return;
    const need = [];
    items.forEach(it => {
      if (it.poster) return;
      const q = it.display_title || it.hot_keyword || it.title || '';
      if (q) need.push(q);
    });
    if (!need.length) return;
    try {
      const r = await fetch('/api/home/posters', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ titles: need.slice(0, 24) }),
      });
      const d = await r.json();
      const map = d.items || {};
      let changed = false;
      items.forEach(it => {
        const q = it.display_title || it.hot_keyword || it.title || '';
        if (!it.poster && map[q]) {
          it.poster = map[q];
          changed = true;
        }
      });
      if (changed) rowEl.innerHTML = items.map(cardHtml).join('');
    } catch (e) {}
  }

  async function loadHomeFeed(appendLatest) {
    const latest = document.getElementById('homeLatestRow');
    const clicks = document.getElementById('homeClickRow');
    const hot = document.getElementById('homeHotRow');
    const moreBtn = document.getElementById('homeLatestMore');
    if (!appendLatest) {
      latestOffset = 0;
      latestItems = [];
      skelGrid(latest, 12);
      skelRow(clicks, 7);
      if (hot) hot.innerHTML = '<span style="color:var(--text-muted);font-size:.8rem">加载中…</span>';
    } else if (moreBtn) {
      moreBtn.disabled = true;
      moreBtn.textContent = '加载中…';
    }
    try {
      const off = appendLatest ? latestOffset : 0;
      const r = await fetch('/api/home/feed?limit=24&offset=' + off);
      const d = await r.json();
      if (hot && !appendLatest) {
        const items = d.hot || [];
        hot.innerHTML = items.length
          ? items.map((it, i) =>
              `<button type="button" class="home-hot-chip" data-q="${esc(it.keyword)}">
                <span class="rank">${i + 1}</span>${esc(it.keyword)}<span class="n">${it.count || ''}</span>
              </button>`
            ).join('')
          : '<span style="color:var(--text-muted);font-size:.8rem">暂无热搜</span>';
        hot.querySelectorAll('.home-hot-chip').forEach(btn => {
          btn.onclick = () => {
            const q = btn.getAttribute('data-q') || '';
            const input = document.getElementById('homeSearchInput');
            if (input) input.value = q;
            runHomeSearch(q);
          };
        });
      }
      if (latest) {
        const batch = d.latest || [];
        latestTotal = d.latest_total != null ? d.latest_total : batch.length;
        latestOffset = d.latest_next_offset != null ? d.latest_next_offset : (off + batch.length);
        if (appendLatest) {
          latestItems = latestItems.concat(batch);
        } else {
          latestItems = batch.slice();
        }
        latest.innerHTML = latestItems.length
          ? latestItems.map(cardHtml).join('')
          : '<span style="color:var(--text-muted);font-size:.8rem;padding:12px">暂无新入库</span>';
        fillMissingPosters(latestItems, latest);
        if (moreBtn) {
          const hasMore = latestOffset < latestTotal;
          moreBtn.hidden = !hasMore;
          moreBtn.disabled = false;
          moreBtn.textContent = '加载更多';
        }
      }
      if (clicks && !appendLatest) {
        const items = d.clicks || [];
        clicks.innerHTML = items.length
          ? items.map(cardHtml).join('')
          : '<span style="color:var(--text-muted);font-size:.8rem;padding:12px">暂无热门</span>';
        fillMissingPosters(items, clicks);
      }
    } catch (e) {
      if (!appendLatest) {
        if (latest) latest.innerHTML = '<span style="color:#f87171;font-size:.8rem">加载失败</span>';
        if (clicks) clicks.innerHTML = '';
        if (hot) hot.innerHTML = '';
      } else if (moreBtn) {
        moreBtn.disabled = false;
        moreBtn.textContent = '加载更多';
      }
    }
  }

  async function loadPersonal() {
    const wrap = document.getElementById('homePersonal');
    const grid = document.getElementById('homePersonalGrid');
    if (!wrap || !grid) return;
    try {
      const r = await fetch('/api/home/personal');
      const d = await r.json();
      if (!d.login) {
        wrap.classList.add('hidden');
        return;
      }
      const pills = [];
      if (d.subscription_new_total > 0) {
        pills.push(`<button type="button" class="home-pill" data-act="subs">订阅新货 <strong>${d.subscription_new_total}</strong></button>`);
      } else if ((d.subscriptions || []).length) {
        pills.push(`<button type="button" class="home-pill" data-act="subs">已订阅 <strong>${d.subscriptions.length}</strong></button>`);
      }
      if ((d.watchlist_ready || []).length) {
        pills.push(`<button type="button" class="home-pill" data-act="wl">想看有源 <strong>${d.watchlist_ready.length}</strong></button>`);
      }
      pills.push(`<button type="button" class="home-pill" data-act="fav" onclick="if(typeof guardFavoritesNav==='function' && !window.VD_IN_GROUP){guardFavoritesNav();return;} location.href='/profile'">收藏 <strong>${d.favorites_count || 0}</strong></button>`);
      if (!pills.length) {
        wrap.classList.add('hidden');
        return;
      }
      wrap.classList.remove('hidden');
      grid.innerHTML = pills.join('');
      grid.querySelectorAll('.home-pill').forEach(btn => {
        const act = btn.getAttribute('data-act');
        if (act === 'subs') {
          btn.onclick = () => {
            const first = (d.subscriptions || []).find(s => s.new_count > 0) || (d.subscriptions || [])[0];
            if (first) runHomeSearch(first.keyword);
          };
        } else if (act === 'wl') {
          btn.onclick = () => {
            const first = (d.watchlist_ready || [])[0];
            if (first) openDetailDrawer(first.title);
          };
        }
      });
    } catch (e) {
      wrap.classList.add('hidden');
    }
  }

  function bindGlobalSearch() {
    const input = document.getElementById('globalSearch');
    const box = input && input.closest('.search-global');
    if (!input || input.dataset.enhanceBound === '1') return;
    input.dataset.enhanceBound = '1';
    if (box && !document.getElementById('globalAcPanel')) {
      const panel = document.createElement('div');
      panel.className = 'global-ac-panel home-ac-panel';
      panel.id = 'globalAcPanel';
      box.appendChild(panel);
    }
    input.addEventListener('input', () => {
      clearTimeout(globalAcTimer);
      globalAcTimer = setTimeout(() => loadGlobalAutocomplete(input.value.trim()), 180);
    });
    input.addEventListener('keydown', (e) => {
      const panel = document.getElementById('globalAcPanel');
      if (e.key === 'ArrowDown' && globalAcItems.length) {
        e.preventDefault();
        globalAcIndex = Math.min(globalAcIndex + 1, globalAcItems.length - 1);
        paintGlobalAc();
      } else if (e.key === 'ArrowUp' && globalAcItems.length) {
        e.preventDefault();
        globalAcIndex = Math.max(globalAcIndex - 1, 0);
        paintGlobalAc();
      } else if (e.key === 'Enter') {
        if (globalAcIndex >= 0 && globalAcItems[globalAcIndex]) {
          e.preventDefault();
          e.stopImmediatePropagation();
          pickGlobalAc(globalAcItems[globalAcIndex].keyword);
        }
      } else if (e.key === 'Escape' && panel) {
        panel.classList.remove('open');
      }
    });
    document.addEventListener('click', (e) => {
      const panel = document.getElementById('globalAcPanel');
      if (panel && box && !box.contains(e.target)) panel.classList.remove('open');
    });
  }

  async function loadGlobalAutocomplete(q) {
    const panel = document.getElementById('globalAcPanel');
    if (!panel) return;
    if (!q) {
      panel.classList.remove('open');
      panel.innerHTML = '';
      globalAcItems = [];
      globalAcIndex = -1;
      return;
    }
    try {
      const r = await fetch('/api/autocomplete?q=' + encodeURIComponent(q));
      const d = await r.json();
      globalAcItems = d.items || [];
      globalAcIndex = -1;
      paintGlobalAc();
    } catch (e) {
      panel.classList.remove('open');
    }
  }

  function paintGlobalAc() {
    const panel = document.getElementById('globalAcPanel');
    if (!panel) return;
    if (!globalAcItems.length) {
      panel.classList.remove('open');
      panel.innerHTML = '';
      return;
    }
    panel.innerHTML = globalAcItems.map((it, i) =>
      `<div class="home-ac-item${i === globalAcIndex ? ' active' : ''}" data-i="${i}">
        <span>${esc(it.keyword || '')}</span>
        <span style="color:var(--text-muted);font-size:.72rem">${it.hit_count != null ? it.hit_count : ''}</span>
      </div>`
    ).join('');
    panel.classList.add('open');
    panel.querySelectorAll('.home-ac-item').forEach(el => {
      el.onclick = () => pickGlobalAc(globalAcItems[Number(el.getAttribute('data-i'))].keyword);
    });
  }

  function pickGlobalAc(keyword) {
    const input = document.getElementById('globalSearch');
    if (input) input.value = keyword;
    document.getElementById('globalAcPanel')?.classList.remove('open');
    if (typeof window.searchResource === 'function') window.searchResource(keyword);
    else if (typeof window.executeSearch === 'function') window.executeSearch();
  }

  function getLocalFavs() {
    try { return JSON.parse(localStorage.getItem(LOCAL_FAV_KEY) || '{}'); }
    catch (e) { return {}; }
  }

  function saveLocalFavs(map) {
    localStorage.setItem(LOCAL_FAV_KEY, JSON.stringify(map));
  }

  function patchFavorites() {
    if (typeof window.toggleFavorite !== 'function' || window.toggleFavorite.__localPatch) return;
    const origToggle = window.toggleFavorite;
    window.toggleFavorite = async function (btn, rid) {
      if (!btn || !rid) return;
      if (window.VD_LOGGED_IN) return origToggle(btn, rid);
      const map = getLocalFavs();
      const key = String(rid);
      const isFav = !!map[key];
      if (isFav) {
        delete map[key];
        btn.classList.remove('is-fav', 'is-local-fav');
        btn.innerHTML = '♡';
        btn.title = '本地收藏';
        if (typeof showToast === 'function') showToast('已取消本地收藏');
      } else {
        map[key] = { id: rid, at: Date.now() };
        btn.classList.add('is-fav', 'is-local-fav');
        btn.innerHTML = '♥';
        btn.title = '取消本地收藏（登录后可同步）';
        if (typeof showToast === 'function') showToast('已加入本地收藏（登录可同步）', 'success');
      }
      saveLocalFavs(map);
    };
    window.toggleFavorite.__localPatch = true;

    if (typeof window.checkFavoriteStatus === 'function' && !window.checkFavoriteStatus.__localPatch) {
      const origCheck = window.checkFavoriteStatus;
      window.checkFavoriteStatus = async function (rids) {
        await origCheck(rids);
        if (window.VD_LOGGED_IN) return;
        const map = getLocalFavs();
        document.querySelectorAll('.fav-btn').forEach(btn => {
          const id = String(btn.getAttribute('data-id') || '');
          if (map[id]) {
            btn.classList.add('is-fav', 'is-local-fav');
            btn.innerHTML = '♥';
            btn.title = '取消本地收藏（登录后可同步）';
          }
        });
      };
      window.checkFavoriteStatus.__localPatch = true;
    }
  }

  function injectSearchAuthHint() {
    const sf = document.getElementById('searchMetaFilters');
    if (!sf || document.getElementById('vdAuthHint')) return;
    const hint = document.createElement('div');
    hint.className = 'vd-auth-hint';
    hint.id = 'vdAuthHint';
    hint.textContent = window.VD_LOGGED_IN
      ? '已登录：收藏会同步到账号'
      : '搜索免费 · 收藏可本地暂存 · 登录后可云端同步';
    sf.parentNode.insertBefore(hint, sf);
  }

  function badgeHtml(info) {
    if (!info) return '';
    const st = info.status || 'none';
    let text = '无源';
    if (st === 'live') text = `有源 ${info.count || ''}`.trim();
    else if (st === 'mixed') text = `部分失效`;
    else if (st === 'dead') text = '已失效';
    const pans = (info.source_labels || []).slice(0, 2).join('/');
    return `
      <div class="poster-badges">
        <span class="poster-badge ${st}">${esc(text)}</span>
        ${pans ? `<span class="poster-badge ${st}">${esc(pans)}</span>` : ''}
      </div>
    `;
  }

  async function enrichPosterBadges(items) {
    if (!window.VD_LOGGED_IN) return {};
    const titles = (items || []).map(it => it.title).filter(Boolean);
    if (!titles.length) return {};
    try {
      const r = await fetch('/api/home/availability', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ titles }),
      });
      if (r.status === 401) return {};
      const d = await r.json();
      return d.items || {};
    } catch (e) {
      return {};
    }
  }

  // monkey-patch renderPosterCard + loadPosterWall
  function patchPosterWall() {
    if (typeof window.renderPosterCard !== 'function') return;
    if (window.renderPosterCard.__enhanced) return;
    const orig = window.renderPosterCard;
    window.renderPosterCard = function (it) {
      const html = orig(it);
      const titleJs = JSON.stringify(it.title || '');
      let out = html.replace(
        /onclick="searchResource\('[\s\S]*?'\)"/,
        `onclick='openDetailDrawer(${titleJs})'`
      );
      if (!out.includes('poster-badges')) {
        out = out.replace(
          '<div class="poster-wrap">',
          `<div class="poster-wrap" data-title="${esc(it.title || '')}"><div class="poster-badges" data-badge-for="${esc(it.title || '')}"></div>`
        );
      }
      // 海报 fallback 升级
      const title = it.title || '';
      const poster = it.poster || it.backdrop || '';
      const thumbInner = posterThumbHtml({
        title,
        poster,
        type: it.type,
        mediaType: it.media_type,
      });
      out = out.replace(
        /<img[^>]*class="poster-item"[\s\S]*?<div style="display:none[^>]*>无海报<\/div>/,
        `<div class="poster-thumb" style="position:absolute;inset:0">${thumbInner}</div>`
      );
      out = out.replace('检索片源', '查看片源');
      out = out.replace(
        /<div class="poster-overlay-title">([^<]*)<\/div>/,
        `<div class="poster-overlay-title" title="${esc(title)}">$1</div>`
      );
      return out;
    };
    window.renderPosterCard.__enhanced = true;

    if (typeof window.loadPosterWall === 'function' && !window.loadPosterWall.__enhanced) {
      const origLoad = window.loadPosterWall;
      window.loadPosterWall = async function (type) {
        await origLoad(type);
        try { stampWallBadges(); } catch (e) {}
      };
      window.loadPosterWall.__enhanced = true;
    }
  }

  function stampWallBadges() {
    const cards = document.querySelectorAll('#posterWallGrid .poster-wall-card');
    if (!cards.length) return;
    const titles = [];
    cards.forEach(card => {
      const wrap = card.querySelector('.poster-wrap');
      if (!wrap) return;
      const title = (card.querySelector('[data-title]') || {}).getAttribute?.('data-title')
        || (card.querySelector('.poster-overlay-title') || {}).textContent
        || '';
      if (!title) return;
      wrap.setAttribute('data-title', title);
      if (!wrap.querySelector('.poster-badges')) {
        const slot = document.createElement('div');
        slot.className = 'poster-badges';
        slot.setAttribute('data-badge-for', title);
        wrap.appendChild(slot);
      }
      card.onclick = function (e) {
        if (e.target.closest && e.target.closest('.poster-fav-btn')) return;
        openDetailDrawer(title);
      };
      titles.push(title);
    });
    enrichPosterBadges(titles.map(t => ({ title: t }))).then(map => {
      document.querySelectorAll('#posterWallGrid .poster-wrap[data-title]').forEach(el => {
        const t = el.getAttribute('data-title');
        const slot = el.querySelector('[data-badge-for]');
        if (slot && map[t]) slot.outerHTML = badgeHtml(map[t]);
      });
    }).catch(() => {});
  }

  window.openDetailDrawer = async function (title) {
    title = (title || '').trim();
    if (!title) return;
    ensureHomeSections();
    const mask = document.getElementById('detailDrawerMask');
    const drawer = document.getElementById('detailDrawer');
    const body = document.getElementById('detailBody');
    const h = document.getElementById('detailTitle');
    const sub = document.getElementById('detailSub');
    if (!mask || !drawer || !body) return;
    h.textContent = title;
    sub.textContent = '加载中…';
    body.innerHTML = '<div class="detail-empty">正在拉取本站片源…</div>';
    mask.classList.add('open');
    drawer.classList.add('open');
    document.body.style.overflow = 'hidden';
    try {
      const r = await fetch('/api/home/title_detail?title=' + encodeURIComponent(title));
      if (r.status === 401) {
        closeDetailDrawer();
        if (typeof showToast === 'function') showToast('片源加载失败，请刷新重试', 'error');
        return;
      }
      const d = await r.json();
      const avail = d.avail || {};
      const items = d.items || [];
      const sources = d.sources || [];
      const panLine = sources.map(s => `${s.label} ${s.count}`).join(' · ');
      sub.textContent = items.length
        ? `本站 ${items.length} 条片源 · ${panLine || '未识别网盘'}`
        : '本站暂无匹配链接';
      body.innerHTML = `
        <div class="detail-actions">
          <button type="button" class="primary" onclick='searchResource(${JSON.stringify(title)})'>全站检索</button>
          <button type="button" onclick="if(typeof guardFavoritesNav==='function' && !window.VD_IN_GROUP){guardFavoritesNav();return;} location.href='/profile'">去收藏夹</button>
        </div>
        <div class="detail-filters" id="detailFilters">
          <button type="button" class="active" data-src="全部">全部 ${items.length}</button>
          ${sources.map(s =>
            `<button type="button" data-src="${esc(s.label)}">${esc(s.label)} ${s.count}</button>`
          ).join('')}
        </div>
        <div id="detailLinkList">
        ${items.length ? items.map(it => {
          const dead = (it.link_status === 'dead') || it.is_valid === 0;
          return `
            <div class="detail-link${dead ? ' is-dead' : ''}" data-src="${esc(it.source_label || it.source || '')}">
              <span class="src">${esc(it.source_label || it.source || '?')}</span>
              <div class="info">
                <div class="t">${esc(it.title || title)}</div>
                <div class="m">${esc([it.quality, it.size, it.year, dead ? '失效' : '可用'].filter(Boolean).join(' · '))}${it.password ? ' · 提取码 ' + esc(it.password) : ''}</div>
              </div>
              ${it.url ? `<a class="go" href="${esc(it.url)}" target="_blank" rel="noopener">打开</a>` : '<span></span>'}
            </div>
          `;
        }).join('') : '<div class="detail-empty">没有找到本站链接，可点「全站检索」继续找</div>'}
        </div>
      `;
      const bar = document.getElementById('detailFilters');
      bar && bar.querySelectorAll('button').forEach(btn => {
        btn.onclick = () => {
          bar.querySelectorAll('button').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          const src = btn.getAttribute('data-src');
          document.querySelectorAll('#detailLinkList .detail-link').forEach(el => {
            el.style.display = (!src || src === '全部' || el.getAttribute('data-src') === src) ? '' : 'none';
          });
        };
      });
    } catch (e) {
      sub.textContent = '加载失败';
      body.innerHTML = `<div class="detail-empty">${esc(e.message || 'error')}</div>`;
    }
  };

  window.closeDetailDrawer = function () {
    document.getElementById('detailDrawerMask')?.classList.remove('open');
    document.getElementById('detailDrawer')?.classList.remove('open');
    document.body.style.overflow = '';
  };


  function setupPwa() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true;
    if (isStandalone) {
      localStorage.setItem('vd_pwa_dismiss', '1');
      document.getElementById('pwaHint')?.remove();
      return;
    }
    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault();
      deferredInstall = e;
      const hint = document.getElementById('pwaHint');
      if (hint && !localStorage.getItem('vd_pwa_dismiss')) hint.classList.add('show');
    });
    window.addEventListener('appinstalled', () => {
      localStorage.setItem('vd_pwa_dismiss', '1');
      document.getElementById('pwaHint')?.classList.remove('show');
    });
    document.getElementById('pwaOk')?.addEventListener('click', async () => {
      if (!deferredInstall) return;
      deferredInstall.prompt();
      await deferredInstall.userChoice;
      deferredInstall = null;
      document.getElementById('pwaHint')?.classList.remove('show');
      localStorage.setItem('vd_pwa_dismiss', '1');
    });
    document.getElementById('pwaNo')?.addEventListener('click', () => {
      localStorage.setItem('vd_pwa_dismiss', '1');
      document.getElementById('pwaHint')?.classList.remove('show');
    });
  }

  function boot() {
    ensureHomeSections();
    patchPosterWall();
    patchFavorites();
    bindGlobalSearch();
    injectSearchAuthHint();
    loadHomeFeed(false);
    loadPersonal();
    setupPwa();
    document.getElementById('homeLatestMore')?.addEventListener('click', () => loadHomeFeed(true));
    // 不重绘海报墙（会骨架屏闪一下、随机打乱、看起来像改布局）
    // 只在已有卡片上盖角标
    setTimeout(() => { try { stampWallBadges(); } catch (e) {} }, 200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 50));
  } else {
    setTimeout(boot, 50);
  }
  // 主页脚本较晚执行时再补丁一次
  window.addEventListener('load', () => {
    patchPosterWall();
    patchFavorites();
    ensureHomeSections();
    bindGlobalSearch();
    injectSearchAuthHint();
  });
})();
