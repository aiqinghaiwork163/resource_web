/**
 * 网站设置动态加载
 * 从数据库读取 site_name 和 site_footer，自动更新页面
 */
(function(){
  fetch('/api/site-settings').then(function(r){return r.json()}).then(function(d){
    var name = d.site_name || '资源雷达';
    var footer = d.site_footer || '🎬 资源雷达 · Powered by Flask';
    
    // 更新页面标题
    var titleEl = document.querySelector('title');
    if(titleEl){
      var oldTitle = titleEl.textContent;
      if(oldTitle.indexOf('资源雷达') > -1 || oldTitle.indexOf('网盘资源浏览器') > -1){
        titleEl.textContent = oldTitle.replace(/资源雷达|网盘资源浏览器/g, name);
      }
    }
    
    // 更新所有 logo 元素
    document.querySelectorAll('.logo').forEach(function(el){
      var icon = el.querySelector('i');
      if(icon){
        el.innerHTML = icon.outerHTML + name;
      } else {
        el.textContent = name;
      }
    });
    
    // 更新 h1 标题
    document.querySelectorAll('h1').forEach(function(el){
      if(el.textContent.indexOf('资源雷达') > -1 || el.textContent.indexOf('网盘资源浏览器') > -1){
        el.textContent = el.textContent.replace(/资源雷达|网盘资源浏览器/g, name);
      }
    });
    
    // 更新页脚
    document.querySelectorAll('.footer').forEach(function(el){
      el.textContent = footer;
    });
    
  }).catch(function(){});
})();
