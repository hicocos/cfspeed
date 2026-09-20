import {createApp} from 'vue'
import App from './App.vue'
import router from './router'
import '@fontsource-variable/geist'
import './style.css'
import './design.css'
import './compact-admin.css'
import './mkw-public.css'
import './mkw-admin.css'
import './cfspeed.css'
const app=createApp(App).use(router)
router.isReady().then(()=>app.mount('#app')).catch(()=>{const node=document.querySelector('#app p');if(node)node.textContent='加载失败，请刷新页面重试。'})
