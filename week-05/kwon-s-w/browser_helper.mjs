import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const requireHere = createRequire(import.meta.url);
let playwright;
try { playwright = requireHere('playwright'); }
catch {
  const bundled = process.env.AUCTION_NODE_MODULES || path.join(process.env.USERPROFILE || '', '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules');
  playwright = createRequire(path.join(bundled, 'package.json'))('playwright');
}
const [mode, inputFile] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
const log = (message) => process.stderr.write(message + '\n');
const options = { headless: true };
const executable = [process.env.AUCTION_BROWSER, playwright.chromium.executablePath(),
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p => p && fs.existsSync(p));
if (executable) options.executablePath = executable;
const browser = await playwright.chromium.launch(options);
const deadline=setTimeout(()=>{
  console.log(JSON.stringify({status:'failed',message:'브라우저 작업 전체 제한 시간(240초) 초과'}));
  browser.close().finally(()=>process.exit(1));
},240000);
deadline.unref();
let page;
try {
  const context = await browser.newContext({ acceptDownloads: true, locale: 'ko-KR', viewport: {width:1440,height:1000} });
  page = await context.newPage();
  page.setDefaultTimeout(25000);
  if (mode === 'courts') {
    await page.goto('https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ159M00.xml', {waitUntil:'domcontentloaded'});
    await page.locator('[id$="sbx_auctnCsSrchCortOfc"] option').filter({hasText:'서울중앙지방법원'}).waitFor({state:'attached'});
    const courts=await page.locator('[id$="sbx_auctnCsSrchCortOfc"] option').evaluateAll(els=>els.map(e=>({name:e.textContent.trim(),search_value:e.value})).filter(e=>e.search_value));
    fs.writeFileSync(input.output,JSON.stringify(courts,null,2));
    console.log(JSON.stringify({status:'saved',courts:courts.length}));
  } else if (mode === 'render') {
    await page.goto(new URL('file:///' + input.html.replaceAll('\\','/')).href);
    await page.emulateMedia({media:'print'});
    await page.pdf({path:input.pdf, format:'A4', printBackground:true, margin:{top:'14mm',bottom:'14mm',left:'12mm',right:'12mm'}});
    console.log(JSON.stringify({status:'saved',path:input.pdf,representation:'rendered_from_html'}));
  } else if (mode === 'specification' || mode === 'lookup') {
    log('법원 사건번호 검색 화면 연결');
    await page.goto('https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ159M00.xml', {waitUntil:'domcontentloaded'});
    const court = page.locator('[id$="sbx_auctnCsSrchCortOfc"]');
    await court.selectOption({label:input.court_name});
    await page.locator('[id$="sbx_auctnCsSrchCsYear"]').selectOption(input.year);
    await page.locator('[id$="ibx_auctnCsSrchCsNo"]').fill(input.serial);
    const caseResponsePromise=page.waitForResponse(r=>r.url().includes('/pgj15A/selectAuctnCsSrchRslt.on'),{timeout:45000});
    await page.locator('[id$="btn_auctnCsSrchBtn"]').click();
    const caseResponse=await caseResponsePromise;
    const caseData=await caseResponse.json();
    if (mode === 'lookup') {
      fs.writeFileSync(input.output,JSON.stringify(caseData));
      console.log(JSON.stringify({status:'saved',path:input.output}));
      await browser.close();
      process.exit(0);
    }
    log('사건 상세에서 요청 물건 선택');
    const detailButtons=page.locator('[id$="_btn_gdsDtlInq"]');
    await detailButtons.first().waitFor({state:'visible'});
    let selected = null;
    for (const button of await detailButtons.all()) {
      const number = await button.evaluate(el => document.getElementById(el.id.replace(/_btn_gdsDtlInq$/, '_spn_temp'))?.textContent?.trim());
      if (String(Number(number)) === String(input.item_number)) { selected = button; break; }
    }
    if (!selected) throw new Error('요청 물건의 상세조회 버튼을 확인하지 못했습니다.');
    if (!await selected.isEnabled()) throw new Error('법원 화면에서 물건상세조회가 비활성 상태입니다.');
    await selected.click();
    const actualCase=page.locator('[id$="spn_gdsDtlSrchUserCsNo"]');
    await actualCase.filter({hasText:input.case_number}).waitFor({state:'visible'});
    const actualItem=(await page.locator('[id$="spn_gdsDtlSrchGdsSeq"]').innerText()).trim();
    if (actualItem !== String(input.item_number)) throw new Error('법원 상세 화면의 물건번호가 요청과 다릅니다.');
    const specButton = page.locator('[id$="btn_dspslGdsSpcfc1"]');
    if (!await specButton.isVisible()) {
      console.log(JSON.stringify({status:'unavailable',message:'법원 화면에서 매각물건명세서 열람 버튼이 제공되지 않습니다.'}));
    } else {
      log('법원 문서뷰어 연결');
      const popupPromise=context.waitForEvent('page',{timeout:30000});
      await specButton.click();
      const popup=await popupPromise;
      await popup.waitForLoadState('domcontentloaded');
      page=popup;
      const save=popup.getByRole('button',{name:'저장',exact:true});
      await save.waitFor({state:'visible',timeout:45000});
      if (!await save.isEnabled()) throw new Error('문서뷰어에서 저장을 허용하지 않는 문서입니다.');
      log('매각물건명세서 원본 PDF 저장');
      await popup.locator('iframe[src*="streamdocs"]').waitFor({state:'attached',timeout:45000});
      await popup.frameLocator('iframe[src*="streamdocs"]').getByText(input.case_number,{exact:false}).first().waitFor({state:'attached',timeout:60000});
      let captureResolve;
      const captured=new Promise(resolve=>{captureResolve=resolve;});
      const watch=(p)=>{
        p.on('download',async d=>{await d.saveAs(input.output);captureResolve(true);});
        p.on('response',async r=>{
          const h=r.headers();
          if((h['content-type']||'').includes('pdf') || (h['content-disposition']||'').includes('.pdf')){
            const b=await r.body().catch(()=>null);
            if(b?.subarray(0,5).toString()==='%PDF-'){fs.writeFileSync(input.output,b);captureResolve(true);}
          }
        });
      };
      for(const p of context.pages())watch(p);
      context.on('page',watch);
      let timer;
      const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('원본 PDF 저장 응답 대기 시간 초과')),180000);});
      await save.click();
      try {await Promise.race([captured,timeout]);}
      finally {clearTimeout(timer);}
      console.log(JSON.stringify({status:'saved',path:input.output,representation:'original_pdf',source:'https://ecfs.scourt.go.kr/sgvo/'}));
    }
  } else throw new Error('지원하지 않는 helper mode');
} catch (error) {
  const diagnostic = path.join(path.dirname(inputFile), 'browser-diagnostic');
  if (page) {
    await page.screenshot({path:diagnostic+'.png',fullPage:true}).catch(()=>{});
    fs.writeFileSync(diagnostic+'.txt',await page.locator('body').innerText().catch(()=>''));
  }
  console.log(JSON.stringify({status:'failed',message:String(error.message).split('\n')[0],diagnostic}));
  process.exitCode=1;
} finally { clearTimeout(deadline); await browser.close(); }
