import {reactive} from 'vue'
export const session=reactive({authenticated:false,username:'',csrf:'',checked:false})
export class APIError extends Error{constructor(message:string,public status:number){super(message)}}
export function errorText(e:unknown){return e instanceof Error?e.message:'请求失败，请重试。'}
export async function api<T>(path:string,options:{method?:string;body?:unknown;signal?:AbortSignal}={}):Promise<T>{
 const controller=new AbortController();const abort=()=>controller.abort();options.signal?.addEventListener('abort',abort,{once:true});const timeout=setTimeout(abort,20000)
 try{const response=await fetch(path,{method:options.method||'GET',credentials:'same-origin',signal:controller.signal,headers:{'Accept':'application/json',...(options.body!==undefined?{'Content-Type':'application/json','X-CSRF-Token':session.csrf}:{})},body:options.body!==undefined?JSON.stringify(options.body):undefined})
 const data=await response.json().catch(()=>({error:'服务返回了无法读取的响应。'}));if(!response.ok){if(response.status===401&&!path.includes('/auth/')){Object.assign(session,{authenticated:false,csrf:'',checked:false});window.dispatchEvent(new Event('session-expired'))}throw new APIError(data.error||'请求失败',response.status)}return data as T
 }catch(e){if(e instanceof DOMException&&e.name==='AbortError'){if(options.signal?.aborted)throw e;throw new Error('请求超时，请检查服务状态。')}throw e}finally{clearTimeout(timeout);options.signal?.removeEventListener('abort',abort)}
}
export async function ensureSession(force=false){if(session.checked&&!force)return session.authenticated;const data=await api<{authenticated:boolean;username:string;csrf:string}>('/api/auth/session');Object.assign(session,data,{checked:true});return session.authenticated}
