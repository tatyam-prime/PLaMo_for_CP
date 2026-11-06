// Custom sites accept a domain or an HTTP(S) URL with optional path wildcards.
export function siteRule(input) {
  let value = String(input || '').trim();
  if (!value.includes('://')) value = 'https://' + value;
  const match = /^(https?):\/\/(\*\.)?([a-z0-9.-]+)(\/[^?#]*)?$/i.exec(value);
  if (!match || !match[3].includes('.') || !match[3].split('.').every(label => /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i.test(label))) throw new Error('example.com または https://example.com/articles/* の形で入力してください。');
  const host = (match[2] || '') + match[3].toLowerCase();
  const path = match[4] && match[4] !== '/' ? match[4] : '/*';
  return { pattern: `${match[1].toLowerCase()}://${host}${path}`, origin: `${match[1].toLowerCase()}://${host}/*` };
}

export function siteMatches(pattern, address) {
  try {
    const rule = siteRule(pattern), url = new URL(address), parsed = /^(https?):\/\/(\*\.)?([^/]+)(\/.*)$/.exec(rule.pattern);
    if (url.protocol !== parsed[1] + ':') return false;
    const host = parsed[3];
    if (url.hostname !== host && !(parsed[2] && url.hostname.endsWith('.' + host))) return false;
    const path = parsed[4].split('*').map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*');
    return new RegExp('^' + path + '$').test(url.pathname + url.search);
  } catch { return false; }
}
