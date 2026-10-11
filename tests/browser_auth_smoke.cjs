// Read disposable credentials and enrollment codes from stdin. Never print them.
const { chromium } = require('/app/node_modules/playwright')
const assert = require('node:assert/strict')
const input = require('node:readline').createInterface({input:process.stdin})[Symbol.asyncIterator]()
const deadline = setTimeout(()=>{console.error('FAIL: browser deadline');process.exit(1)},150000)
async function main() {
  const {origin,username,password} = JSON.parse((await input.next()).value)
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']})
  let stage = 'anonymous access'; let page
  try {
    const context = await browser.newContext()
    async function denied(context, path='/') {
      const response = await context.request.get(origin+path,{maxRedirects:0,headers:{Accept:'text/html','Remote-User':username}})
      assert.ok([302,303].includes(response.status()), 'authentication required: '+response.status())
    }
    for(const path of ['/','/chat/','/agent/','/images/','/images/api/sessions']) await denied(context,path)
    assert.equal((await context.request.get(origin+'/_dotagents/status')).status(),403)
    for(const path of ['/admin/','/api/authz/forward-auth','/api/health'])
      assert.equal((await context.request.get(origin+':8443'+path)).status(),403)
    console.log('PASS: anonymous access, forged identity headers, and public management are blocked')
    page = await context.newPage(); page.setDefaultTimeout(20000)
    const cdp = await context.newCDPSession(page)
    await cdp.send('WebAuthn.enable')
    const {authenticatorId} = await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{
      protocol:'ctap2',transport:'usb',hasResidentKey:false,hasUserVerification:true,
      isUserVerified:true,automaticPresenceSimulation:true,
    }})
    await page.goto(origin)
    stage = 'incorrect password'
    await page.locator('#username-textfield').fill(username)
    await page.locator('#password-textfield').fill('deliberately-wrong-password')
    const rejected = page.waitForResponse(r=>r.url().endsWith('/api/firstfactor') && r.request().method()==='POST')
    await page.getByRole('button',{name:/sign in/i,exact:true}).click()
    assert.equal((await rejected).status(),401)
    await denied(context,'/images/')
    console.log('PASS: incorrect passwords are rejected')
    stage = 'password without a key'
    await page.locator('#password-textfield').fill(password)
    await page.getByRole('button',{name:/sign in/i,exact:true}).click()
    await page.getByText('Register device',{exact:true}).waitFor()
    await denied(context,'/images/')
    console.log('PASS: a correct password alone cannot open applications')
    stage = 'security key enrollment'
    await page.getByText('Register device',{exact:true}).click()
    await page.waitForLoadState('networkidle')
    await page.getByRole('button',{name:'Add',exact:true}).click()
    await page.locator('#one-time-code').waitFor()
    console.log('NEED_CODE')
    const {code} = JSON.parse((await input.next()).value)
    await page.locator('#one-time-code').fill(code)
    await page.getByRole('button',{name:'Verify',exact:true}).click()
    await page.locator('#webauthn-credential-description').fill('Disposable browser test key')
    await page.locator('#dialog-next').click()
    await page.getByText('Disposable browser test key',{exact:true}).waitFor()
    const {credentials} = await cdp.send('WebAuthn.getCredentials',{authenticatorId})
    assert.equal(credentials.length,1)
    console.log('PASS: local verification and FIDO2 key registration succeed')
    stage = 'security key login'
    await page.goto(origin)
    await page.waitForURL(url=>url.origin===origin && url.pathname==='/',{timeout:30000})
    await page.getByRole('heading',{name:'Services',exact:true}).waitFor()
    for(const [name,path] of [['Chat','/chat/'],['Agent','/agent/']]) {
      await page.goto(origin)
      await page.getByRole('link',{name:'Open '+name,exact:true}).click()
      await page.getByText(name,{exact:true}).waitFor()
      assert.equal(new URL(page.url()).pathname,path)
      assert.equal((await context.request.get(origin+path+'_dotagents/status')).status(),403)
    }
    console.log('PASS: the dashboard opens Chat and Agent through one authenticated session')
    stage = 'shared Image session'
    const image = await context.newPage()
    await image.goto(origin+'/images/')
    await image.getByRole('textbox',{name:'Prompt',exact:true}).waitFor()
    assert.equal((await context.request.get(origin+'/images/api/sessions')).status(),200)
    assert.equal((await context.request.post(origin+'/images/api/generate',{
      headers:{Origin:'https://foreign.invalid'},data:{},
    })).status(),403)
    const cookies = await context.cookies()
    const auth = cookies.find(c=>c.name==='__Secure-dotagents-authelia')
    assert.ok(auth?.secure && auth?.httpOnly && auth?.sameSite==='Lax')
    console.log('PASS: one secure session opens Image; foreign origins are blocked')
    stage = 'application cookie bypass'
    const isolated = await browser.newContext()
    await isolated.addCookies(cookies.filter(c=>c.name.startsWith('dsh-auth-')))
    await denied(isolated)
    console.log('PASS: application cookies cannot bypass Authelia')
    stage = 'shared logout'
    await page.goto(origin+':8443/')
    await page.getByText('Logout',{exact:true}).click()
    await page.locator('#username-textfield').waitFor()
    await denied(context)
    await denied(context,'/images/')
    console.log('PASS: logout closes access to all applications')
    stage = 'later login needs the key'
    await cdp.send('WebAuthn.setAutomaticPresenceSimulation',{authenticatorId,enabled:false})
    await page.goto(origin)
    await page.locator('#username-textfield').fill(username)
    await page.locator('#password-textfield').fill(password)
    await page.getByRole('button',{name:/sign in/i,exact:true}).click()
    await page.getByText('Security Key',{exact:true}).waitFor()
    await denied(context)
    await cdp.send('WebAuthn.setAutomaticPresenceSimulation',{authenticatorId,enabled:true})
    await page.reload()
    await page.waitForURL(url=>url.origin===origin && url.pathname==='/',{timeout:30000})
    await page.getByRole('heading',{name:'Services',exact:true}).waitFor()
    console.log('PASS: later login requires key presence and accepts the registered key')
  } catch(e) {
    console.error('FAIL:',stage,e.message)
    if(page) console.error('PAGE:',await page.locator('body').innerText())
    process.exitCode=1
  } finally {await browser.close()}
}
main().then(()=>{clearTimeout(deadline);process.exit(process.exitCode||0)}).catch(()=>{console.error('FAIL: initialization');process.exit(1)})
