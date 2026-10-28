import {defineConfig} from 'vite';
export default defineConfig({base:'/purchase/',build:{outDir:'../service/static/purchase',emptyOutDir:true,rollupOptions:{input:'purchase/index.html'}},server:{proxy:{'/api':'http://127.0.0.1:5199'}}});
