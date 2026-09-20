import { nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { gsap } from 'gsap'

/** Progressive enhancement: content stays visible without motion or JavaScript animation. */
export function usePageMotion() {
  const route = useRoute()
  let media: gsap.MatchMedia | undefined
  let observer: MutationObserver | undefined
  let generation = 0
  let active = true

  function stop() {
    observer?.disconnect()
    observer = undefined
    media?.revert()
    media = undefined
  }

  async function enter() {
    const current = ++generation
    stop()
    await nextTick()
    if (!active || current !== generation) return
    // Lazy route components may resolve after the router state changes.
    const animate = () => {
      const root = document.querySelector<HTMLElement>('#main-content')
      if (!root || !root.querySelector('h1')) return false
      observer?.disconnect()
      media = gsap.matchMedia()
      media.add('(prefers-reduced-motion: no-preference)', () => {
        const intro = root.querySelectorAll('[data-reveal]')
        const heading = root.querySelector('.admin-heading, .page-intro, .login-card, .empty-state')
        const timeline = gsap.timeline({ defaults: { duration: 0.6, ease: 'power3.out' } })
        timeline.addLabel('intro')
        if (intro.length) timeline.from(intro, { y: 22, stagger: 0.075, clearProps: 'opacity,transform' }, 'intro')
        else if (heading) timeline.from(heading, { y: 12, clearProps: 'opacity,transform' }, 'intro')
        timeline.addLabel('content', 0.12)
        const content = root.querySelector('.stat-grid, .docs-layout, .image-filters, .upload-layout, .settings-layout')
        if (content) timeline.from(content, { y: 10, clearProps: 'opacity,transform' }, 'content')
      }, root)
      return true
    }
    // Apply entrance transforms in the mount microtask, before the first paint.
    {
      if (!active || current !== generation || animate()) return
      observer = new MutationObserver(() => { if (active && current === generation) animate() })
      observer.observe(document.getElementById('app')!, { childList: true, subtree: true })
    }
  }

  onMounted(enter)
  watch(() => route.path, enter, { flush: 'post' })
  onBeforeUnmount(() => { active = false; generation++; stop() })
}
