const semver = require(process.argv[2]);
const vs = require('fs').readFileSync(process.argv[3], 'utf8').trim().split('\n');
const out = [];
for (const a of vs) for (const b of vs) out.push(Math.sign(semver.compare(a, b, {loose:false})));
console.log(JSON.stringify(out));
