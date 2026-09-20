<script setup lang="ts">
import Icon from '../components/Icon.vue'
import SiteLogo from '../components/SiteLogo.vue'
</script>

<template>
  <main id="main-content" class="public-home">
    <header class="public-home-header">
      <RouterLink to="/" class="home-brand" aria-label="cfspeed 首页">
        <span class="home-brand-mark"><SiteLogo :size="24" /></span>
        <span>cfspeed<small>DNS 同步控制台</small></span>
      </RouterLink>
      <RouterLink to="/admin/login" class="home-login">进入管理台 <Icon name="arrow" :size="16" /></RouterLink>
    </header>

    <section class="home-hero" aria-labelledby="home-title">
      <div class="home-copy">
        <span class="home-eyebrow">CFSPEED / DNS SYNC</span>
        <h1 id="home-title">让优选 IP<br><em>按计划更新</em></h1>
        <p>统一管理优选 IP 来源、Cloudflare 与 DNSPod 解析目标。先预览，再同步，保持每次变更可核对。</p>
        <div class="home-actions">
          <RouterLink to="/admin/login" class="home-primary">进入管理台 <Icon name="arrow" :size="18" /></RouterLink>
          <RouterLink to="/admin/docs" class="home-secondary">查看使用文档</RouterLink>
        </div>
      </div>
      <div class="home-console-preview" aria-label="控制台功能概览">
        <div class="preview-bar"><span><i /><i /><i /></span><small>cfspeed / 控制台</small><span class="preview-status"><b />运行中</span></div>
        <div class="preview-body">
          <div class="preview-rail"><span class="active"><Icon name="chart" :size="17" /></span><span><Icon name="target" :size="17" /></span><span><Icon name="history" :size="17" /></span><span><Icon name="gear" :size="17" /></span></div>
          <div class="preview-content">
            <small>OVERVIEW</small><strong>总览与统计</strong>
            <div class="preview-metrics"><div><b>预览</b><span>运行模式</span></div><div><b>6 h</b><span>运行间隔</span></div></div>
            <div class="preview-line"><span /><span /><span /></div>
            <div class="preview-note"><Icon name="shield" :size="17" /><span>正式同步前可先核对 DNS 目标</span></div>
          </div>
        </div>
      </div>
    </section>

    <section class="home-features" aria-label="功能特点">
      <article><span class="feature-icon"><Icon name="globe" :size="20" /></span><h2>多来源</h2><p>读取现有优选 IP 接口，支持三网来源。</p></article>
      <article><span class="feature-icon"><Icon name="shield" :size="20" /></span><h2>先预览</h2><p>默认不修改 DNS，确认后再启用正式同步。</p></article>
      <article><span class="feature-icon"><Icon name="clock" :size="20" /></span><h2>按计划</h2><p>运行间隔可按小时设置，默认每 6 小时一次。</p></article>
    </section>

    <footer class="home-footer">cfspeed <span>优选 IP 与 DNS 同步管理</span></footer>
  </main>
</template>

<style scoped>
.public-home{min-height:100dvh;color:#203d63;background:linear-gradient(135deg,#f1f7fd 0%,#eaf3fc 52%,#f7fbff 100%);padding:28px max(24px,5vw) 24px;overflow:hidden}.public-home-header{width:min(1184px,100%);margin:auto;display:flex;align-items:center;justify-content:space-between}.home-brand{display:inline-flex;align-items:center;gap:11px;color:#203d63;font-size:22px;font-weight:750;letter-spacing:.015em}.home-brand small{display:block;font-size:8px;letter-spacing:.14em;font-weight:500;color:#566d88;margin-top:3px}.home-brand-mark{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;color:#fff;background:linear-gradient(145deg,#4086c9,#2463ad);box-shadow:0 4px 12px #2463ad20,inset 0 1px #ffffff40}.home-login{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border:1px solid #b6cee6;border-radius:8px;color:#28578b;background:#ffffffb8;font-size:13px}.home-login:hover{background:#eaf3ff;color:#194e8d}.home-hero{width:min(1080px,100%);margin:0 auto;min-height:560px;display:grid;grid-template-columns:minmax(0,1fr) minmax(360px,490px);align-items:center;gap:clamp(40px,8vw,120px);padding:76px 0 66px}.home-eyebrow{color:#2463ad;font-size:11px;font-weight:700;letter-spacing:.16em}.home-copy h1{font-size:clamp(44px,6vw,72px);line-height:1.16;letter-spacing:-.065em;margin:20px 0 22px;color:#203d63}.home-copy h1 em{font-style:normal;color:#2463ad}.home-copy p{max-width:500px;color:#566d88;font-size:16px;line-height:1.9;margin-bottom:28px}.home-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.home-primary,.home-secondary{display:inline-flex;align-items:center;gap:9px;min-height:46px;padding:11px 18px;border-radius:8px;font-size:13px}.home-primary{color:#fff;background:#2463ad;box-shadow:inset 0 1px #ffffff26}.home-primary:hover{background:#194e8d}.home-secondary{color:#28578b;border:1px solid #b6cee6;background:#ffffff80}.home-secondary:hover{background:#eaf3ff}.home-console-preview{border:1px solid #c5d9ed;border-radius:17px;background:#ffffffed;box-shadow:0 24px 60px #355a8517,inset 0 1px #fff;transform:rotate(1.5deg)}.preview-bar{height:48px;display:flex;align-items:center;gap:14px;padding:0 18px;border-bottom:1px solid #e1ebf5;color:#566d88}.preview-bar>span:first-child{display:flex;gap:5px}.preview-bar i{width:7px;height:7px;border-radius:50%;background:#a9c8e6}.preview-bar small{font-size:11px;flex:1}.preview-status{font-size:10px;display:flex;align-items:center;gap:5px}.preview-status b{width:6px;height:6px;border-radius:50%;background:#579276}.preview-body{display:flex;min-height:286px}.preview-rail{width:62px;padding:20px 10px;border-right:1px solid #e1ebf5;display:flex;align-items:center;flex-direction:column;gap:13px}.preview-rail span{width:36px;height:34px;display:grid;place-items:center;border-radius:7px;color:#7c9ab7}.preview-rail .active{color:#194e8d;background:#dcecff;box-shadow:inset 3px 0 #4b8dce}.preview-content{flex:1;padding:30px 28px}.preview-content>small{font-size:9px;letter-spacing:.16em;color:#2463ad}.preview-content>strong{display:block;font-size:24px;letter-spacing:-.04em;margin:7px 0 25px}.preview-metrics{display:grid;grid-template-columns:1fr 1fr;gap:12px}.preview-metrics div{padding:15px;border:1px solid #d5e3f1;border-radius:9px;background:#f7fbff}.preview-metrics b,.preview-metrics span{display:block}.preview-metrics b{font-size:20px;color:#2463ad}.preview-metrics span{font-size:10px;color:#566d88;margin-top:4px}.preview-line{display:flex;gap:7px;margin:24px 0}.preview-line span{height:5px;border-radius:5px;background:#d5e7f7}.preview-line span:first-child{width:48%;background:#73a8d7}.preview-line span:nth-child(2){width:29%}.preview-line span:last-child{width:17%}.preview-note{display:flex;align-items:center;gap:8px;padding:11px 12px;border-radius:7px;color:#405c7b;background:#edf5ff;font-size:11px}.home-features{width:min(1080px,100%);margin:0 auto;display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.home-features article{padding:23px;border:1px solid #d5e3f1;border-radius:12px;background:#ffffffb8;box-shadow:0 8px 28px #355a8508}.feature-icon{width:38px;height:38px;display:grid;place-items:center;border-radius:9px;color:#4075ab;background:#eaf3ff;margin-bottom:16px}.home-features h2{font-size:18px;margin-bottom:6px}.home-features p{color:#566d88;font-size:13px;line-height:1.7;margin:0}.home-footer{width:min(1080px,100%);margin:46px auto 0;padding-top:20px;border-top:1px solid #d5e3f1;color:#566d88;font-size:12px}.home-footer span{margin-left:12px;color:#8298af}@media(max-width:760px){.public-home{padding:20px 18px}.home-brand{font-size:20px}.home-brand-mark{width:37px;height:37px;border-radius:10px}.home-login{font-size:12px;padding:9px 10px}.home-hero{grid-template-columns:1fr;gap:36px;padding:62px 0 45px}.home-copy p{font-size:14px}.home-console-preview{width:min(100%,490px);margin:auto}.home-features{grid-template-columns:1fr;gap:12px}.home-features article{padding:18px}.home-footer{margin-top:32px}}@media(max-width:400px){.public-home-header{align-items:flex-start}.home-brand small{font-size:7px}.home-login{font-size:11px}.home-hero{padding-top:48px}.home-copy h1{font-size:45px}.preview-content{padding:24px 16px}.preview-content>strong{font-size:20px}.preview-rail{width:48px;padding-inline:6px}.preview-rail span{width:34px}.home-footer span{display:block;margin:5px 0 0}}
</style>
