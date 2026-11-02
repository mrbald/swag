import assert from 'node:assert/strict';
import {test} from 'node:test';
import {deflateSync, constants} from 'node:zlib';
import {preflightClipboard} from '../web/clipboard.js';

const LIMIT = 1024 * 1024;
// Pinned noVNC uses unescape(encodeURIComponent(text)); it throws URIError for
// malformed UTF-16 instead of substituting a replacement character.
const encodeUTF8 = text => unescape(encodeURIComponent(text));
const deflate = data => deflateSync(data, {finishFlush: constants.Z_FULL_FLUSH});
const check = (text, compressor = deflate) => preflightClipboard(text, encodeUTF8, compressor);

test('empty clear and normalized ASCII preserve the noVNC byte envelope', () => {
  const payloads = [];
  for (const text of ['', 'A\rB\nC\r\nD']) {
    assert.deepEqual(check(text, data => {payloads.push(data); return deflate(data);}), {ok: true});
  }
  assert.deepEqual([...payloads[0]], [0, 0, 0, 1, 0]);
  assert.equal(Buffer.from(payloads[1].subarray(4, -1)).toString(), 'A\r\nB\r\nC\r\nD');
  assert.equal(new DataView(payloads[1].buffer).getUint32(0), payloads[1].length - 4);
});

test('declared boundary counts UTF-8, CRLF expansion and terminal NUL', () => {
  assert.equal(check('a'.repeat(LIMIT - 1)).ok, true);
  assert.equal(check('a'.repeat(LIMIT)).ok, false);
  assert.equal(check('€'.repeat(349525)).ok, true); // 1,048,575 UTF-8 bytes.
  assert.equal(check('€'.repeat(349526)).ok, false);
  assert.equal(check('\n'.repeat(524287)).ok, true); // 1,048,574 bytes after CRLF.
  assert.equal(check('\n'.repeat(524288)).ok, false);
});

test('embedded NUL and malformed UTF-16 reject before compression', () => {
  const noCompress = () => {throw new Error('must not compress');};
  assert.match(check('left\0right', noCompress).error, /null character/);
  assert.match(check('left\ud800right', noCompress).error, /unsupported character/);
  assert.match(check('\udc00', noCompress).error, /unsupported character/);
  assert.equal(check('😀').ok, true);
});

test('wire boundary includes four flag bytes and later valid text still passes', () => {
  assert.equal(check('valid', () => new Uint8Array(LIMIT - 4)).ok, true);
  assert.match(check('valid', () => new Uint8Array(LIMIT - 3)).error, /too large/);
  assert.deepEqual(check('next valid'), {ok: true});
});
