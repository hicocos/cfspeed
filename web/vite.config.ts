import {defineConfig} from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins:[vue()],build:{sourcemap:false,target:'es2022',chunkSizeWarningLimit:400},server:{proxy:{'/api':'http://127.0.0.1:8788','/ipTop.html':'http://127.0.0.1:8788'}}})
