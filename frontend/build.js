const fs = require('fs');
const path = require('path');

const root = __dirname;
const buildDir = path.join(root, 'build');
const srcDir = path.join(root, 'src');

fs.rmSync(buildDir, { recursive: true, force: true });
fs.mkdirSync(path.join(buildDir, 'static', 'js'), { recursive: true });
fs.mkdirSync(path.join(buildDir, 'static', 'css'), { recursive: true });

fs.copyFileSync(path.join(srcDir, 'index.html'), path.join(buildDir, 'index.html'));
fs.copyFileSync(path.join(srcDir, 'app.js'), path.join(buildDir, 'static', 'js', 'app.js'));
fs.copyFileSync(path.join(srcDir, 'style.css'), path.join(buildDir, 'static', 'css', 'style.css'));

console.log('Frontend build created in frontend/build');
