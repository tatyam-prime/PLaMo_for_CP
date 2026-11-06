// Verified document roots and source languages shared by the core and settings.
export const SITE_REGISTRY = [
  { id: 'codeforces', name: 'Codeforces', homepage: 'https://codeforces.com/', host: /(^|\.)codeforces\.com$/, roots: '.problem-statement', sourceLanguage: 'English' },
  { id: 'codechef', name: 'CodeChef', homepage: 'https://www.codechef.com/', host: /^(www\.)?codechef\.com$/, roots: '#problem-statement', sourceLanguage: 'English', waitForStatement: true },
  { id: 'cses', name: 'CSES', homepage: 'https://cses.fi/', host: /^cses\.fi$/, roots: '.content .md, .task-content', sourceLanguage: 'English' },
  { id: 'kattis', name: 'Kattis', homepage: 'https://open.kattis.com/', host: /(^|\.)kattis\.com$/, roots: '.problembody', sourceLanguage: 'English' },
  { id: 'repovive', name: 'Repovive', homepage: 'https://repovive.com/', host: /^repovive\.com$/, roots: '.prose', problemPath: /^\/contests\/[^/]+\/problems\/[^/]+/, sourceLanguage: 'English', waitForStatement: true, restriction: repoviveRestriction,
    notice: { text: 'MathRound のAI翻訳は禁止されています。既存のサイト内翻訳をご利用ください。', url: 'https://repovive.com/contests/1015/rules', label: '大会規定' } },
  { id: 'uoj', name: 'UOJ', homepage: 'https://uoj.ac/', host: /^uoj\.ac$/, roots: '#tab-statement .uoj-article', sourceLanguage: 'Chinese' },
  { id: 'tlx', name: 'TLX', homepage: 'https://tlx.toki.id/', host: /^tlx\.toki\.id$/, roots: '.programming-problem-statement__text .html-text', sourceLanguage: 'auto', waitForStatement: true },
  { id: 'project-euler', name: 'Project Euler', homepage: 'https://projecteuler.net/', host: /^(www\.)?projecteuler\.net$/, roots: '#content .problem_content', sourceLanguage: 'English' },
  { id: 'arxiv', name: 'arXiv', homepage: 'https://arxiv.org/', host: /^arxiv\.org$/, roots: 'article.ltx_document', problemPath: /^\/html\//, documentType: 'general', sourceLanguage: 'English' },
];

export function problemRoots(site, page = document, address = location) {
  if (!site || site.problemPath && !site.problemPath.test(address.pathname)) return [];
  const roots = Array.from(page.querySelectorAll(site.roots));
  return roots.filter(root => !roots.some(other => other !== root && other.contains(root)));
}

function repoviveRestriction(address, page) {
  if (!/\/problems\//.test(address.pathname)) return null;
  const eventPattern = /\bMath\s*(?:Contest\s*)?Round\b|\bMath\s+Contest\b/i;
  const pathContest = /^\/contests\/([^/]+)\/problems\//.exec(address.pathname)?.[1];
  const breadcrumbs = Array.from(page.querySelectorAll('a[data-slot="breadcrumb-link"]')).filter(link => /^\/contests\/[^/]+\/?$/.test(new URL(link.href, address.href).pathname));
  const mathBreadcrumb = breadcrumbs.find(link => eventPattern.test(link.textContent));
  // Ignore a problem named "Math ..."; title matching uses the contest suffix.
  const eventTitle = page.title.includes(' - ') ? page.title.slice(page.title.lastIndexOf(' - ') + 3) : page.title;
  // Round 1's numeric alias is verified even before its SPA title arrives.
  if (pathContest !== '1015' && !/^m\d+$/i.test(pathContest || '') && !eventPattern.test(eventTitle) && !mathBreadcrumb) return null;
  const breadcrumbContest = mathBreadcrumb ? /^\/contests\/([^/]+)/.exec(new URL(mathBreadcrumb.href, address.href).pathname)?.[1] : undefined;
  const contest = pathContest || breadcrumbContest;
  return { message: 'MathRound では翻訳バーを自動表示しません。利用時は大会規定を確認してください。',
    url: contest ? `https://repovive.com/contests/${encodeURIComponent(contest)}/rules` : undefined };
}

export function translationRestriction() {
  const site = SITE_REGISTRY.find(item => item.host.test(location.hostname));
  return typeof site?.restriction === 'function' ? site.restriction(location, document) : null;
}
