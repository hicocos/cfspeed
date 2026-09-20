<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount, useId } from 'vue'
import Icon from './Icon.vue'
const props = defineProps<{ open: boolean; title: string; busy?: boolean }>(); const emit = defineEmits<{ close: [] }>(); const dialog = ref<HTMLDialogElement>()
const titleId = useId()
let previous: HTMLElement | null = null
function sync() { if (props.open && !dialog.value?.open) { previous = document.activeElement as HTMLElement; dialog.value?.showModal() } else if (!props.open && dialog.value?.open) { dialog.value.close(); previous?.focus() } }
watch(() => props.open, sync); onMounted(sync); onBeforeUnmount(() => dialog.value?.close())
function close() { if (!props.busy) emit('close') }
</script><template><dialog ref="dialog" class="modal" :aria-labelledby="titleId" @cancel.prevent="close" @click="e => { if(e.target === dialog) close() }"><div class="modal-inner"><header><h2 :id="titleId">{{ title }}</h2><button type="button" class="icon-btn" aria-label="关闭对话框" :disabled="busy" @click="close"><Icon name="close" /></button></header><slot /></div></dialog></template>