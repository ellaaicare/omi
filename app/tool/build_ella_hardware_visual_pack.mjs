#!/usr/bin/env node

import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const toolDir = dirname(fileURLToPath(import.meta.url));
const appDir = resolve(toolDir, '..');
const packDir = join(appDir, 'assets/images/ella-hardware');
const sourceDir = join(packDir, 'source');
const pngDir = join(packDir, 'png');
const svgDir = join(packDir, 'svg');

for (const dir of [sourceDir, pngDir, svgDir]) mkdirSync(dir, { recursive: true });

const palette = {
  paper: '#FAF6F0',
  card: '#F2EBE1',
  cardDeep: '#E9DFD2',
  ink: '#23201C',
  inkSoft: '#665F56',
  teal: '#5A9E8F',
  tealDeep: '#38695E',
  amber: '#8B6914',
};

const devices = [
  {
    key: 'necklace-omi',
    onSource: join(appDir, 'assets/images/omi-without-rope.webp'),
    offSource: join(appDir, 'assets/images/omi-without-rope-turned-off.webp'),
    contentBox: 54,
  },
  {
    key: 'headset-whisper',
    onSource: join(sourceDir, 'headset-whisper-master.png'),
    offSource: join(sourceDir, 'headset-whisper-master.png'),
    contentBox: 58,
  },
];

const states = ['on', 'off', 'reconnecting', 'low-battery'];
const scales = [1, 2, 3];

function magick(args) {
  execFileSync('magick', args, { stdio: 'inherit' });
}

function writeSvgFromPng(key, state, pngPath) {
  const payload = readFileSync(pngPath).toString('base64');
  const label = `${key} ${state}`;
  writeFileSync(
    join(svgDir, `${key}-${state}.svg`),
    `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" role="img" aria-label="${label}">
  <image width="64" height="64" href="data:image/png;base64,${payload}"/>
</svg>
`,
  );
}

function renderDeviceState(device, state, scale) {
  const size = 64 * scale;
  const box = device.contentBox * scale;
  const isOffTreatment = state === 'off' || state === 'reconnecting';
  const source = isOffTreatment ? device.offSource : device.onSource;
  const output = join(pngDir, `${device.key}-${state}${scale === 1 ? '' : `@${scale}x`}.png`);

  const args = [
    source,
    '-background',
    'none',
    '-trim',
    '+repage',
    '-resize',
    `${box}x${box}`,
    '-gravity',
    'center',
    '-extent',
    `${size}x${size}`,
  ];

  if (isOffTreatment) {
    args.push('-colorspace', 'Gray', '-channel', 'A', '-evaluate', 'multiply', '0.45', '+channel');
  }

  if (state === 'reconnecting') {
    const outerRadius = 5 * scale;
    const innerRadius = 2.5 * scale;
    const cx = 54 * scale;
    const cy = 53 * scale;
    args.push(
      '-fill',
      'none',
      '-stroke',
      palette.cardDeep,
      '-strokewidth',
      `${1.5 * scale}`,
      '-draw',
      `circle ${cx},${cy} ${cx + outerRadius},${cy}`,
      '-fill',
      palette.teal,
      '-stroke',
      'none',
      '-draw',
      `circle ${cx},${cy} ${cx + innerRadius},${cy}`,
    );
  }

  if (state === 'low-battery') {
    const left = 22 * scale;
    const top = 58 * scale;
    const right = 42 * scale;
    const bottom = 61 * scale;
    const radius = 1.5 * scale;
    args.push(
      '-fill',
      palette.amber,
      '-stroke',
      'none',
      '-draw',
      `roundrectangle ${left},${top} ${right},${bottom} ${radius},${radius}`,
    );
  }

  args.push('-strip', '-define', 'png:exclude-chunk=date,time', output);
  magick(args);
  return output;
}

for (const device of devices) {
  for (const state of states) {
    let svgSource;
    for (const scale of scales) {
      const output = renderDeviceState(device, state, scale);
      if (scale === 3) svgSource = output;
    }
    writeSvgFromPng(device.key, state, svgSource);
  }
}

const necklaceGlyph = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none">
  <title>OMI necklace</title>
  <path d="M12 2.75C7.305 2.75 3.5 6.555 3.5 11.25S7.305 19.75 12 19.75s8.5-3.805 8.5-8.5S16.695 2.75 12 2.75Z" stroke="${palette.ink}" stroke-width="1.7"/>
  <path d="M12 5.15a6.1 6.1 0 1 0 0 12.2 6.1 6.1 0 0 0 0-12.2Z" stroke="${palette.inkSoft}" stroke-width="1.35"/>
  <path d="M9.8 20.05 12 22l2.2-1.95" stroke="${palette.ink}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="12" cy="11.25" r="1.2" stroke="${palette.tealDeep}" stroke-width="1.5"/>
</svg>
`;

const headsetGlyph = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none">
  <title>Ella whisper headset</title>
  <path d="M4.7 15.6C3.4 10.2 6.8 5.6 12.4 5.1c5.6-.5 9.4 3.2 8 7.3-.9 2.7-3.5 3.5-6.3 4.3" stroke="${palette.ink}" stroke-width="2" stroke-linecap="round"/>
  <path d="M3.2 16.1c1.3-.5 2.7.4 3.2 2s-.2 3.2-1.5 3.6-2.7-.4-3.2-2 .2-3.2 1.5-3.6Z" stroke="${palette.ink}" stroke-width="2" stroke-linejoin="round"/>
  <path d="M11.9 16.9c1.4-.4 2.8.5 3.2 2.1s-.4 3.1-1.8 3.5-2.8-.5-3.2-2.1.4-3.1 1.8-3.5Z" stroke="${palette.ink}" stroke-width="2" stroke-linejoin="round"/>
</svg>
`;

writeFileSync(join(svgDir, 'necklace-omi-glyph.svg'), necklaceGlyph);
writeFileSync(join(svgDir, 'headset-whisper-glyph.svg'), headsetGlyph);

for (const key of ['necklace-omi', 'headset-whisper']) {
  for (const scale of scales) {
    const suffix = scale === 1 ? '' : `@${scale}x`;
    const output = join(pngDir, `${key}-glyph${suffix}.png`);
    execFileSync(
      'sips',
      [
        '-z',
        `${24 * scale}`,
        `${24 * scale}`,
        '-s',
        'format',
        'png',
      join(svgDir, `${key}-glyph.svg`),
        '--out',
        output,
      ],
      { stdio: 'ignore' },
    );
    magick([
      output,
      '-strip',
      '-define',
      'png:exclude-chunk=date,time',
      output,
    ]);
  }
}

const manifest = {
  version: 1,
  pointSizes: {
    tile: 64,
    glyph: 24,
  },
  scales,
  palette,
  devices: {
    'necklace-omi': {
      deviceType: 'omi',
      sourceOn: '../omi-without-rope.webp',
      sourceOff: '../omi-without-rope-turned-off.webp',
      sourcePolicy: 'Shipping OMI renders, resized only; never redrawn.',
    },
    'headset-whisper': {
      deviceType: 'whisperHeadset',
      sourceReference: 'source/headset-system-reference.webp',
      sourceMaster: 'source/headset-whisper-master.png',
      sourcePolicy: 'Generated once from the Ella system reference photo; state exports are deterministic.',
    },
  },
  states: {
    on: { treatment: 'fullColor' },
    off: { treatment: 'grayscale', opacity: 0.45 },
    reconnecting: {
      treatment: 'offWithBreathingDotSlot',
      breathingDot: { centerPt: [54, 53], outerRadiusPt: 5, innerRadiusPt: 2.5, color: palette.teal },
    },
    'low-battery': {
      treatment: 'onWithAmberCaptionSlot',
      captionColor: palette.amber,
      note: 'App renders localized caption text; artwork includes the amber caption anchor bar only.',
    },
  },
  naming: {
    svg: '<device-key>-<state>.svg',
    png1x: '<device-key>-<state>.png',
    png2x: '<device-key>-<state>@2x.png',
    png3x: '<device-key>-<state>@3x.png',
    glyph: '<device-key>-glyph.{svg,png,@2x.png,@3x.png}',
  },
  constraints: ['transparent backgrounds', 'no red', 'hardware-agnostic DeviceType resolution'],
};

writeFileSync(join(packDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
writeFileSync(
  join(packDir, 'README.md'),
  `# Ella hardware visual pack

Tile artwork is exported at 64pt in SVG and PNG at 1×/2×/3×. Settings/connect glyphs are 24pt.

- \`necklace-omi-*\` preserves the shipping OMI renders in \`app/assets/images\`.
- \`headset-whisper-*\` is based on Ella's public system reference photo.
- OFF is grayscale at 45% opacity.
- Reconnecting reserves a lower-right breathing-dot slot.
- Low-battery keeps the ON artwork and exposes the amber caption anchor/color.
- The app should resolve artwork by \`DeviceType\`; do not hard-code generic necklace/headset art.

Run \`node tool/build_ella_hardware_visual_pack.mjs\` from \`app/\` to regenerate deterministic exports.
`,
);

console.log(`Built Ella hardware visual pack in ${packDir}`);
