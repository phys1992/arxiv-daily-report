#!/usr/bin/env node
/**
 * 把一批 LaTeX 公式渲染成自包含的 SVG（MathJax v3，本地字体缓存）。
 *
 * 用法： node render.js <in.json> [fontCache] <out.json>
 *   in.json : [{"id":"1","tex":"F(R)","display":false}, ...]
 *   out.json: [{"id":"1","tex":"F(R)","svg":"<svg .../>"}, ...]
 *
 * SVG 使用 MathJax 的 Computer Modern 字形轮廓 —— 与 XeLaTeX 输出视觉一致。
 */
const fs = require('fs');
const path = require('path');

const { mathjax } = require('mathjax-full/js/mathjax.js');
const { TeX } = require('mathjax-full/js/input/tex.js');
const { SVG } = require('mathjax-full/js/output/svg.js');
const { liteAdaptor } = require('mathjax-full/js/adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');
const { AllPackages } = require('mathjax-full/js/input/tex/AllPackages.js');

function main() {
  const [, , inFile, fontCache = 'local', outFile] = process.argv;
  if (!inFile || !outFile) {
    console.error('用法: node render.js <in.json> [fontCache] <out.json>');
    process.exit(2);
  }
  const adaptor = liteAdaptor();
  RegisterHTMLHandler(adaptor);
  const tex = new TeX({ packages: AllPackages });
  const svg = new SVG({ fontCache });
  const doc = mathjax.document('', { InputJax: tex, OutputJax: svg });

  const items = JSON.parse(fs.readFileSync(inFile, 'utf8'));
  const out = items.map((it) => {
    try {
      const node = doc.convert(it.tex, {
        display: !!it.display,
        em: 16,
        ex: 8,
        containerWidth: 800,
      });
      return { id: it.id, tex: it.tex, display: !!it.display, svg: adaptor.innerHTML(node) };
    } catch (err) {
      return { id: it.id, tex: it.tex, error: String(err && err.message ? err.message : err) };
    }
  });
  fs.writeFileSync(outFile, JSON.stringify(out));
  const ok = out.filter((o) => o.svg).length;
  console.log(`rendered ${ok}/${out.length}`);
}

main();
