import {createRouter,createWebHistory} from 'vue-router'
import {ensureSession} from './lib/api'
import AdminLayout from './AdminLayout.vue'
const router=createRouter({history:createWebHistory(),scrollBehavior:()=>({top:0}),routes:[
 {path:'/',component:()=>import('./pages/Home.vue'),meta:{title:'首页'}},{path:'/admin/login',component:()=>import('./pages/Login.vue'),meta:{title:'管理员登录'}},
 {path:'/admin',component:AdminLayout,children:[{path:'',redirect:'/admin/stats'},
 {path:'stats',component:()=>import('./pages/Overview.vue'),meta:{title:'总览与统计'}},
 {path:'targets',component:()=>import('./pages/Targets.vue'),meta:{title:'DNS 目标'}},
 {path:'history',component:()=>import('./pages/History.vue'),meta:{title:'同步记录'}},
 {path:'settings',component:()=>import('./pages/Settings.vue'),meta:{title:'服务设置'}},
 {path:'profile',component:()=>import('./pages/Profile.vue'),meta:{title:'账户安全'}},
 {path:'docs',component:()=>import('./pages/Docs.vue'),meta:{title:'使用文档'}}]},
 {path:'/:pathMatch(.*)*',component:()=>import('./pages/NotFound.vue'),meta:{title:'页面不存在'}}]})
router.beforeEach(async to=>{if(to.path==='/'||to.path==='/admin/login')return;try{if(!await ensureSession())return{path:'/admin/login',query:{next:to.fullPath}}}catch{return{path:'/admin/login',query:{error:'无法连接服务，请稍后重试。'}}}})
router.afterEach(to=>{document.title=`${to.meta.title||'控制台'} · cfspeed`})
window.addEventListener('session-expired',()=>{void router.replace({path:'/admin/login',query:{expired:'1'}})})
export default router
