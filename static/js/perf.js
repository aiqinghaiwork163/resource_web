/* 性能优化：减少动画 + Visibility API 暂停 + PWA 注册 */
(function () {
  'use strict';

  /* prefers-reduced-motion: 禁用/简化 CSS 动画 */
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    document.documentElement.classList.add('reduce-motion');
  }
  window.matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', function (e) {
    document.documentElement.classList.toggle('reduce-motion', e.matches);
  });

  /* Visibility API: 页面不可见时暂停动画 */
  document.addEventListener('visibilitychange', function () {
    document.documentElement.classList.toggle('page-hidden', document.hidden);
  });

  /* Service Worker 注册 */
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/static/sw.js').catch(function () {});
    });
  }
})();
