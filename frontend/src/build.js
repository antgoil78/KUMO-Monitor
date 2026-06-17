const fs = require('fs');
const path = require('path');

const sourceDir = path.join(__dirname, 'src');
const buildDir = path.join(__dirname, 'build');

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });

  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);

    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

fs.rmSync(buildDir, { recursive: true, force: true });
copyDir(sourceDir, buildDir);
console.log('Frontend build created in ./build');
