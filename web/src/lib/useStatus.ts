import {onMounted,onBeforeUnmount,ref} from 'vue'
import {api,errorText} from './api'
import type {Status} from '../types'
export function useStatus(){const status=ref<Status>();const error=ref('');const loading=ref(false);let active=true;let timer:ReturnType<typeof setTimeout>;let controller:AbortController|undefined
 async function load(){if(loading.value)return;loading.value=true;controller=new AbortController();try{const next=await api<Status>('/api/admin/status',{signal:controller.signal});if(active){status.value=next;error.value=''}}catch(e){if(active&&!(e instanceof DOMException&&e.name==='AbortError'))error.value=errorText(e)}finally{loading.value=false}}
 async function poll(){await load();if(active)timer=setTimeout(poll,5000)}onMounted(poll);onBeforeUnmount(()=>{active=false;clearTimeout(timer);controller?.abort()});return{status,error,loading,load}}
