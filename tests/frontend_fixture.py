"""Explicit frontend fixture test. Real backend flows are verified separately."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/'artifacts'
STYLE="e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return{background:s.backgroundColor,color:s.color,border:s.border,borderRadius:s.borderRadius,padding:s.padding,fontSize:s.fontSize,fontFamily:s.fontFamily,width:r.width,height:r.height,x:r.x,y:r.y}}"
STYLE_KEYS=['background','color','border','borderRadius','padding','fontSize','fontFamily']
refs={kind:json.loads((OUT/f'reference-{kind}-metrics.json').read_text()) for kind in ['login','admin']}
config={'revision':0,'service':{'source_url':'https://ip.164746.xyz/ipTop.html','interval_seconds':21600,'timeout_seconds':15,'attempts':3,'dry_run':True,'max_ips':30,'pushplus_token_env':''},'targets':[],'credentials':[]}
status={'version':'fixture','status':'ok','ips':[],'ready':True,'dry_run':True,'interval_seconds':21600,'history':[],'targets':[],'runs':0,'pending_operations':[]}
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    report={'mocked_api':True,'differences':[],'pages':[],'browser_errors':[]}
    for width,height in [(1440,1000),(390,844),(320,780)]:
        page=browser.new_page(viewport={'width':width,'height':height},reduced_motion='reduce')
        page.on('pageerror',lambda error:report['browser_errors'].append(str(error)))
        def fixture(route):
            path=route.request.url.split('/api/')[-1]
            data={'auth/session':{'authenticated':True,'username':'admin','csrf':'fixture'},'admin/config':config,'admin/status':status}.get(path,{})
            route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
        page.route('**/api/**',fixture)
        for path,heading in [('login','管理员登录'),('stats','总览与统计'),('targets','DNS 目标'),('history','同步记录'),('settings','服务设置'),('profile','账户安全'),('docs','使用文档')]:
            page.goto('http://127.0.0.1:18789/admin/'+path,wait_until='networkidle')
            page.get_by_role('heading',name=heading,exact=True).wait_for()
            page.evaluate('document.fonts.ready')
            overflow=page.evaluate('document.documentElement.scrollWidth>innerWidth')
            report['pages'].append({'width':width,'path':path,'overflow':overflow})
            if str(width) in refs['login'] and path in ('login','stats'):
                kind='login' if path=='login' else 'admin'
                for selector,expected in refs[kind][str(width)].items():
                    actual=page.locator(selector).first.evaluate(STYLE)
                    keys=STYLE_KEYS+(['width','height','x','y'] if kind=='login' and selector!='.brand-mark' else ['width'] if selector not in ['.admin-main','.setup-banner'] else [])
                    for key in keys:
                        a,b=actual[key],expected[key]
                        same=abs(a-b)<=1 if isinstance(a,(int,float)) and isinstance(b,(int,float)) else a==b
                        if not same:report['differences'].append({'width':width,'selector':selector,'property':key,'reference':b,'target':a})
                page.screenshot(path=str(OUT/f'cfspeed-{path}-fixture-{width}.png'),full_page=True,animations='disabled')
            if path=='targets':
                page.get_by_role('button',name='添加目标',exact=True).first.click()
                page.get_by_role('dialog').wait_for()
                report['pages'].append({'width':width,'path':'target-modal','overflow':page.get_by_role('dialog').evaluate('e=>e.scrollWidth>e.clientWidth')})
                page.get_by_label('服务商',exact=True).select_option('dnspod')
                page.get_by_label('所属域名',exact=True).fill('example.com')
                page.get_by_role('button',name='取消',exact=True).click()
                assert not page.get_by_role('dialog').is_visible()
            if path=='stats' and width<761:
                page.get_by_role('button',name='展开管理导航').click()
                page.locator('.admin-sidebar.open').wait_for()
                page.keyboard.press('Escape')
                assert not page.locator('.admin-sidebar.open').count()
        page.close()
    browser.close()
(OUT/'frontend-fixture-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2))
assert not report['browser_errors']
assert not any(row['overflow'] for row in report['pages'])
