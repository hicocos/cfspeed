<script setup lang="ts">
import { computed } from 'vue'
import { PhVideoCamera, PhImage, PhArrowRight, PhArrowClockwise, PhBookOpen, PhSquaresFour, PhUploadSimple, PhFolder, PhDatabase, PhClock, PhKey, PhChartBar, PhShieldCheck, PhListBullets, PhGearSix, PhUser, PhSignOut, PhPlus, PhX, PhCheck, PhCopy, PhList, PhCode, PhGlobe, PhLightning, PhLock, PhArrowUpRight, PhMagnifyingGlass } from '@phosphor-icons/vue'
const props = withDefaults(defineProps<{ name?: string; size?: number }>(), { name: 'image', size: 20 })
const icons: Record<string, typeof PhImage> = {
  video: PhVideoCamera, image: PhImage, arrow: PhArrowRight, refresh: PhArrowClockwise, book: PhBookOpen,
  grid: PhSquaresFour, upload: PhUploadSimple, folder: PhFolder, database: PhDatabase,
  clock: PhClock, key: PhKey, chart: PhChartBar, shield: PhShieldCheck, list: PhListBullets,
  settings: PhGearSix, user: PhUser, logout: PhSignOut, plus: PhPlus, close: PhX,
  check: PhCheck, copy: PhCopy, menu: PhList, code: PhCode, globe: PhGlobe,
  bolt: PhLightning, lock: PhLock, external: PhArrowUpRight, search: PhMagnifyingGlass,
}
const glyph = computed(() => icons[props.name] || PhImage)
</script>
<template><component :is="glyph" :size="size" weight="regular" aria-hidden="true" /></template>
