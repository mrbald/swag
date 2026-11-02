// Match the pinned noVNC 1.7 ExtendedClipboard Provide path. Qt accepts at
// most 1 MiB of declared UTF-8 text (including trailing NUL) plus its 4-byte
// length prefix, and at most 1 MiB of wire payload (4-byte flags, zlib stream).
const MAX_CLIPBOARD_BYTES = 1024 * 1024;
const TOO_LARGE = 'Clipboard text is too large (1 MiB limit).';

export function preflightClipboard(text, encodeUTF8, deflate) {
  if (text.includes('\0'))
    return {ok: false, error: 'Clipboard text contains an unsupported null character.'};

  // UTF-8 is never shorter than the input's UTF-16 code-unit count. Reject
  // before allocating a larger normalized copy of an obviously oversized input.
  if (text.length > MAX_CLIPBOARD_BYTES - 1)
    return {ok: false, error: TOO_LARGE};

  // noVNC converts lone CR/LF and CRLF to CRLF before adding its terminal NUL.
  const normalized = text.replace(/\r\n|\r|\n/gm, '\r\n');
  let utf8;
  try {
    utf8 = encodeUTF8(normalized);
  } catch (error) {
    if (error instanceof URIError)
      return {ok: false, error: 'Clipboard text contains an unsupported character.'};
    throw error;
  }

  if (utf8.length > MAX_CLIPBOARD_BYTES - 1)
    return {ok: false, error: TOO_LARGE};

  const declared = utf8.length + 1; // Includes noVNC's terminal NUL.
  const inflated = new Uint8Array(4 + declared);
  inflated[0] = declared >>> 24;
  inflated[1] = declared >>> 16;
  inflated[2] = declared >>> 8;
  inflated[3] = declared;
  for (let i = 0; i < utf8.length; i++)
    inflated[i + 4] = utf8.charCodeAt(i);

  // noVNC creates a fresh Deflator for each Provide and sends the 4-byte
  // action/format flags before its compressed stream. Check that exact result.
  if (deflate(inflated).length + 4 > MAX_CLIPBOARD_BYTES)
    return {ok: false, error: TOO_LARGE};

  // A legacy server receives one Latin-1 byte per code point. Its length is
  // no greater than the UTF-8 length above, even for substituted characters.
  return {ok: true};
}
