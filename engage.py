"""Build a private engagement dashboard from note notifications in Gmail."""
import argparse
import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape, unescape
import json
import os
from pathlib import Path
import re
import sqlite3

JST = timezone(timedelta(hours=9))
SUBJECTS = {
    '記事がスキされました！': 'like',
    '記事にコメントがつきました！': 'comment',
    'フォローされました！': 'follow',
}
SCOPE = ['https://www.googleapis.com/auth/gmail.readonly']


def messages(service, account):
    api = service.users().messages()
    query = ('in:anywhere from:noreply@note.com to:' + account +
             ' {' + ' '.join(f'subject:"{subject}"' for subject in (*SUBJECTS, 'さんにスキされました')) + '}')
    request = api.list(userId='me', q=query, includeSpamTrash=True, maxResults=500)
    while request is not None:
        page = request.execute(num_retries=5)
        yield from (item['id'] for item in page.get('messages', []))
        request = api.list_next(request, page)


def body_text(part, mime_type):
    if part.get('mimeType') == mime_type:
        data = part.get('body', {}).get('data', '')
        return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', 'replace')
    for child in part.get('parts', []):
        if body := body_text(child, mime_type):
            return body
    return ''


def parse(message, author):
    part = message['payload']
    headers = {h['name'].lower(): h['value'] for h in part.get('headers', [])}
    subject = headers.get('subject', '').strip()
    legacy_like = re.fullmatch(r'(.+)さんにスキされました[！!]?', subject)
    kind = 'like' if legacy_like else SUBJECTS.get(subject)
    if not kind:
        return None
    body = body_text(part, 'text/plain')
    html = body_text(part, 'text/html') if not body else ''
    person = re.search(r'▼クリエイターページをみる\s+https://note\.com/([a-zA-Z0-9_]+)(?=[?\s]|$)', body)
    if not person and html:
        profiles = re.finditer(r'https:%2F%2Fnote\.com%2F([a-zA-Z0-9_]+)(?=%3F|/\d/)', html, re.I)
        person = next((match for match in profiles if match[1].lower() != author), None)
    # The first article link is the target of the notification.
    article = re.search(r'https://note\.com/(?:notes|' + re.escape(author) + r'/n)/(n[a-f0-9]+)', body)
    if not article and html:
        article = re.search(r'https:%2F%2Fnote\.com%2F(?:notes|' + re.escape(author)
                            + r'%2Fn)%2F(n[a-f0-9]+)', html, re.I)
    intro = (re.match(r'\s*(.*?)さんが(?:あなたを)?フォローしました', body or message.get('snippet', '')) if kind == 'follow'
             else re.match(r'\s*(.*?)さんが「(.*?)」に(?:スキ|コメント)しました', body, re.S))
    title = intro[2] if intro and kind != 'follow' else ''
    if not title and article and html:
        link = re.search(r'<a\b[^>]*href="[^"]*%2F' + article[1] + r'[^"]*"[^>]*>(.*?)</a>', html, re.I | re.S)
        if link:
            title = unescape(re.sub(r'<[^>]+>', '', link[1])).strip()
    return {
        'kind': kind, 'time': int(message['internalDate']),
        'person': person[1].lower() if person else '', 'name': intro[1] if intro else (legacy_like[1] if legacy_like else ''),
        'article': article[1] if article and kind != 'follow' else '', 'title': title,
    }


def render(events, author, follower_ids):
    # Match on note IDs and count distinct articles. Replies to comments and
    # likes on comments are separate email types and are excluded.
    events = sorted(events, key=lambda event: event['time'])
    latest = events[-1]['time'] if events else 0
    cutoff = latest - 30 * 86400 * 1000
    names, titles, follows = {}, {}, set()
    skipped = 0
    for event in events:
        person, article = event['person'], event['article']
        if not person or (event['kind'] != 'follow' and not article):
            skipped += 1
            continue
        if person == author:
            continue
        if event['name']:
            names[person] = event['name']
        if article and event['title']:
            titles[article] = event['title']
        if event['kind'] == 'follow':
            follows.add(person)

    def table(since):
        people = defaultdict(lambda: {'like': set(), 'comment': set()})
        articles = defaultdict(set)
        for event in events:
            kind, person, article = event['kind'], event['person'], event['article']
            if event['time'] < since or kind == 'follow' or not person or not article or person == author:
                continue
            people[person][kind].add(article)
            articles[article].add(person)
        def order(item):
            person, counts = item
            return (-len(counts['like'] | counts['comment']), -len(counts['comment']), person)
        def article_link(article):
            return f'<a href="https://note.com/{author}/n/{article}">{escape(titles.get(article, article))}</a>'
        rows = []
        for person, counts in sorted(people.items(), key=order):
            liked, commented = counts['like'], counts['comment']
            reacted = liked | commented
            label = ('現フォロワー' if person in follower_ids else '一覧に該当なし') if follower_ids is not None else ('フォロー通知あり' if person in follows else 'フォロー通知なし')
            rows.append(f'<tr><td><a href="https://note.com/{person}">{escape(names.get(person, person))}</a>'
                        f'<td>{len(reacted)}<td>{len(liked)}<td>{len(commented)}<td>{label}'
                        f'<td><details><summary>記事を見る</summary>{"<br>".join(article_link(a) for a in sorted(reacted))}</details>')
        denominator = follower_ids if follower_ids is not None else follows
        matched = len(denominator.intersection(people))
        pct = f'{matched}/{len(denominator)}人（{matched/len(denominator):.1%}）' if denominator else '算出不可'
        metric = 'フォロワー反応率' if follower_ids is not None else 'フォロー通知で確認できた人の反応率'
        top_articles = ''.join(f'<li>{article_link(a)} · {len(users)}人</li>' for a, users in sorted(articles.items(), key=lambda item: (-len(item[1]), item[0]))[:20])
        return (f'<p>反応者 {len(people)}人 · {metric} {pct}</p><table><thead><tr><th>読者<th>反応記事<th>スキ記事'
                f'<th>コメント記事<th>フォロー確認<th>内訳</thead><tbody>{"".join(rows)}</tbody></table>'
                f'<h3>反応した人数が多い記事</h3><ol>{top_articles}</ol>')

    as_of = datetime.fromtimestamp(latest / 1000, JST).strftime('%Y-%m-%d %H:%M') if latest else '通知なし'
    return f"""<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Engage Viewer</title><style>
body{{font:16px system-ui;margin:32px auto;padding:0 16px;max-width:1050px;background:#fcfbf8;color:#292333}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}
a{{color:#53417a}}summary{{cursor:pointer}}
</style><h1>Engage Viewer 🌙</h1>
<p>通知の最終受信 {as_of} JST · 照合できなかった通知 {skipped}通</p>
<p>反応記事数はスキとコメントを合わせた記事数です。フォロー通知は過去の履歴です。閲覧のみの読者は集計できません。</p>
<details open><summary>直近30日（最終通知が基準）</summary>{table(cutoff)}</details>
<details><summary>全期間</summary>{table(0)}</details>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True, help='note通知を受け取るGmailアドレス')
    parser.add_argument('--author', default='nero_notelover')
    parser.add_argument('--credentials', type=Path, default=Path('credentials.json'))
    parser.add_argument('--token', type=Path, default=Path('.gmail-token.json'))
    parser.add_argument('--cache', type=Path, default=Path('.engage-cache.sqlite3'))
    parser.add_argument('--output', type=Path, default=Path('engagement.html'))
    parser.add_argument('--followers', type=Path, help='任意。現在のnote IDを1行に1件')
    args = parser.parse_args()
    args.author = args.author.lower()
    if not re.fullmatch(r'[a-z0-9_]+', args.author):
        parser.error('不正なnote IDです')
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(args.token), SCOPE) if args.token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        creds = InstalledAppFlow.from_client_secrets_file(str(args.credentials), SCOPE).run_local_server(port=0)
    fd = os.open(args.token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(creds.to_json())
    service = build('gmail', 'v1', credentials=creds)
    api = service.users().messages()
    ids = dict.fromkeys(messages(service, args.account))
    events = []
    fd = os.open(args.cache, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    with sqlite3.connect(args.cache) as cache:
        cache.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        for message_id in ids:
            key = f'v2:{args.account}:{args.author}:{message_id}'
            cached = cache.execute('SELECT data FROM events WHERE id = ?', (key,)).fetchone()
            if cached:
                event = json.loads(cached[0])
            else:
                message = api.get(userId='me', id=message_id, format='full').execute(num_retries=5)
                event = parse(message, args.author)
                cache.execute('INSERT OR REPLACE INTO events VALUES (?, ?)', (key, json.dumps(event)))
                cache.commit()
            if event:
                events.append(event)
    followers = None
    if args.followers:
        followers = {line.strip().lower() for line in args.followers.read_text().splitlines() if line.strip()}
    args.output.write_text(render(events, args.author, followers), encoding='utf-8')
    print(f'{len(events)}/{len(ids)}件 → {args.output}')


if __name__ == '__main__':
    main()
