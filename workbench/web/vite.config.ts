import {defineConfig} from 'vite';
export default defineConfig({base:'/sales/',build:{outDir:'../service/static/sales',emptyOutDir:true},server:{proxy:{'/api':'http://127.0.0.1:5199'}}});
