// Run after: pnpm --dir web exec tsc src/lib/interval.ts --target es2022 --module es2022 --outDir /tmp/cfspeed-interval-unit
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
const source = readFileSync('/tmp/cfspeed-interval-unit/interval.js', 'utf8')
const {hoursToSeconds,minutesToSeconds} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)
for (let seconds=30; seconds<=604800; seconds++) {
  assert.equal(hoursToSeconds(String(seconds/3600)),seconds)
  assert.equal(minutesToSeconds(String(seconds/60)),seconds)
}
for (const value of ['', ' ', NaN, Infinity, -1, 0, 169, '0.0084', '1.00001']) {
  assert.throws(()=>hoursToSeconds(value))
}
assert.equal(hoursToSeconds('6'),21600)
assert.equal(hoursToSeconds('1.5'),5400)
console.log('PASS: every integer second 30–604800 round-trips; invalid/fractional seconds rejected; 6 h and 1.5 h exact')

for (const value of ['', ' ', NaN, Infinity, -1, 0, 10081, '0.501', '1.00001']) assert.throws(()=>minutesToSeconds(value))
assert.equal(minutesToSeconds('1.5'),90)
console.log('PASS: minute round-trips, bounds and fractional seconds validated')
