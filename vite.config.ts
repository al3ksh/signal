import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// Fonts are always emitted as files: the CSP allows font-src 'self', not data: URIs.
export default defineConfig({plugins:[react()],build:{assetsInlineLimit:(file)=>/\.(woff2?|ttf|otf)$/.test(file)?false:undefined},server:{proxy:{'/api':'http://127.0.0.1:8091'}}});
