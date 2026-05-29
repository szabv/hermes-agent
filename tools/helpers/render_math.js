#!/usr/bin/env node
'use strict';

const fs = require('fs');
const sharp = require('sharp');
const { mathjax } = require('mathjax-full/js/mathjax.js');
const { TeX } = require('mathjax-full/js/input/tex.js');
const { SVG } = require('mathjax-full/js/output/svg.js');
const { liteAdaptor } = require('mathjax-full/js/adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');
const { AllPackages } = require('mathjax-full/js/input/tex/AllPackages.js');

function readStdin() {
  return fs.readFileSync(0, 'utf8');
}

function fail(message, extra = {}) {
  process.stdout.write(JSON.stringify({ success: false, error: message, ...extra }));
  process.exit(0);
}

async function main() {
  let input;
  try {
    input = JSON.parse(readStdin());
  } catch (err) {
    fail(`Invalid JSON input: ${err.message}`);
    return;
  }

  const latex = String(input.latex || '').trim();
  if (!latex) {
    fail('latex is required');
    return;
  }

  const outputPng = String(input.output_png || '');
  const outputSvg = String(input.output_svg || '');
  if (!outputPng || !outputSvg) {
    fail('output_png and output_svg are required');
    return;
  }

  const display = input.display !== false;
  const density = Math.max(72, Math.min(Number(input.density || 300), 600));
  const padding = Math.max(0, Math.min(Number(input.padding ?? 20), 120));
  const background = String(input.background || '#ffffff');

  try {
    const adaptor = liteAdaptor();
    RegisterHTMLHandler(adaptor);

    const tex = new TeX({ packages: AllPackages });
    const svg = new SVG({ fontCache: 'none' });
    const html = mathjax.document('', { InputJax: tex, OutputJax: svg });

    const node = html.convert(latex, { display });
    let inner = adaptor.outerHTML(node)
      .replace(/<mjx-container[^>]*>/, '')
      .replace(/<\/mjx-container>\s*$/, '');

    if (!inner.includes('xmlns="http://www.w3.org/2000/svg"')) {
      inner = inner.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" ');
    }

    fs.mkdirSync(require('path').dirname(outputPng), { recursive: true });
    fs.mkdirSync(require('path').dirname(outputSvg), { recursive: true });
    fs.writeFileSync(outputSvg, inner);

    const info = await sharp(Buffer.from(inner), { density })
      .png()
      .flatten({ background })
      .trim({ background, threshold: 10 })
      .extend({ top: padding, bottom: padding, left: padding + 4, right: padding + 4, background })
      .toFile(outputPng);

    process.stdout.write(JSON.stringify({
      success: true,
      png: outputPng,
      svg: outputSvg,
      width: info.width,
      height: info.height,
      size: info.size,
    }));
  } catch (err) {
    fail(`Math render failed: ${err.message}`, { stack: err.stack });
  }
}

main();
