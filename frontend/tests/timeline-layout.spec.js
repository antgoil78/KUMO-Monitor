import { test, expect } from './timeline.fixture.js'

async function assertConnectorAlignment(page) {
  const errors = await page.locator('.ot-connection').evaluateAll(groups => groups.flatMap(group => {
    const parent = document.querySelector('[data-execution-id="' + group.dataset.parentExecution + '"]'), child = document.querySelector('[data-execution-id="' + group.dataset.childExecution + '"]');
    const path = group.querySelector('.ot-connection-line'), matrix = path.getScreenCTM(), first = path.getPointAtLength(0).matrixTransform(matrix), last = path.getPointAtLength(path.getTotalLength()).matrixTransform(matrix);
    const p = parent.getBoundingClientRect(), c = child.getBoundingClientRect();
    return Math.abs(first.y - p.bottom) > 1 || Math.abs(last.y - c.top) > 1 || Math.abs(last.x - c.left) > 1 ? [JSON.stringify({id: group.dataset.childExecution, first: {x:first.x,y:first.y}, last: {x:last.x,y:last.y}, p:{top:p.top,bottom:p.bottom}, c:{top:c.top,left:c.left}})] : [];
  }));
  if (errors.length) throw Error('Connector alignment: ' + errors.join('; '));
}

test('week range uses day zoom with horizontal scrolling and aligned relationships', async ({ page, timeline }) => {
  const { wfId, monitor, requests } = timeline
  const wf = monitor.rows[0]
  monitor.rows.push(...Array.from({length: 18}, (_, index) => ({ ...wf, WORKFLOW_ID: `empty-${index}`, WORKFLOW_NAME: `Additional workflow ${index + 1}`, run: {status: 'NOT RUN'}, lastRun: {status: 'NOT RUN'}, jobs: [], progress: {total: 0, succeeded: 0, failed: 0, skipped: 0} })))
  await page.goto('/?page=orchestrationMonitor&view=timeline')
  await expect(page.getByLabel('Timeline range', {exact:true})).toHaveValue('604800000')
  await expect(page.getByLabel('Timeline zoom', {exact:true})).toHaveValue('86400000')
  await expect(page.locator('.ot-window')).toContainText('1 day visible · 7 days total')
  await expect(page.locator('.ot-window')).toContainText('displayed')
  const metrics = () => page.locator('.ot-viewport').evaluate(node => {
    const css=getComputedStyle(node), bar=document.querySelector('.ot-scrollbar')
    return {width:node.clientWidth, label:parseFloat(css.getPropertyValue('--ot-label-width')), track:parseFloat(css.getPropertyValue('--ot-track-width')), x:node.scrollLeft, max:node.scrollWidth-node.clientWidth, height:node.clientHeight, scrollHeight:node.scrollHeight, overflowY:css.overflowY, barX:Number(bar.value), barMax:Number(bar.max)}
  })
  let m=await metrics()
  if (Math.abs(m.track/(m.width-m.label)-7)>.02) throw Error('The viewport is not exactly one day at week range: '+JSON.stringify(m))
  if (m.max<1000 || Math.abs(m.barMax-m.max)>2 || Math.abs(m.barX-m.x)>1) throw Error('Missing or mismatched horizontal scrollbar: '+JSON.stringify(m))
  if (m.height<m.scrollHeight-1 || m.overflowY!=='hidden') throw Error('Internal vertical scrolling remains: '+JSON.stringify(m))
  if (m.height<1000) throw Error('Chart did not expand to show all rows: '+JSON.stringify(m))
  const historyRequests=requests.filter(req=>req.path.endsWith('/history'))
  if (!historyRequests.length || historyRequests.some(req=>Date.parse(req.query.end)-Date.parse(req.query.start)<604800000)) throw Error('Full week of history was not requested')
  await page.getByRole('button',{name:'Expand all',exact:true}).click()
  await expect(page.locator('.ot-connection')).toHaveCount(4)
  const labelLeft=await page.locator('.ot-head .ot-label').evaluate(node=>node.getBoundingClientRect().left)
  await page.locator('.ot-scrollbar').focus(); await page.keyboard.press('Home')
  await expect.poll(async()=> (await metrics()).x).toBe(0)
  await page.locator('.ot-scrollbar').focus(); await page.keyboard.press('PageUp')
  await expect.poll(async()=> Math.abs((await metrics()).barX-(await metrics()).x)).toBeLessThan(1)
  if (Math.abs((await page.locator('.ot-head .ot-label').evaluate(node=>node.getBoundingClientRect().left))-labelLeft)>1) throw Error('Workflow labels moved while scrolling')
  await assertConnectorAlignment(page)
  await page.locator('.ot-viewport').evaluate(node=>{node.scrollLeft+=120})
  await expect.poll(async()=> Math.abs((await metrics()).barX-(await metrics()).x)).toBeLessThan(1)
  const requestCount=requests.filter(req=>req.path.endsWith('/history')).length
  await page.getByLabel('Timeline zoom',{exact:true}).selectOption('43200000')
  await expect(page.locator('.ot-window')).toContainText('12h 0m visible · 7 days total')
  await page.waitForTimeout(250)
  m=await metrics()
  if (Math.abs(m.track/(m.width-m.label)-14)>.03) throw Error('12-hour zoom changed the loaded week or scale: '+JSON.stringify(m))
  if (requests.filter(req=>req.path.endsWith('/history')).length!==requestCount) throw Error('Changing zoom reloaded history')
  await page.getByRole('button',{name:'Zoom out timeline',exact:true}).click()
  await expect(page.getByLabel('Timeline zoom',{exact:true})).toHaveValue('86400000')
  await page.locator('.ot-scrollbar').focus(); await page.keyboard.press('Home')
  await page.locator('.ot-scrollbar').focus(); await page.keyboard.press('ArrowRight')
  await expect.poll(async()=> (await metrics()).x).toBeGreaterThan(0)
  await page.getByLabel('Timeline zoom',{exact:true}).selectOption('fit')
  await expect.poll(async()=> (await metrics()).max).toBeLessThan(2)
  await page.getByLabel('Timeline zoom',{exact:true}).selectOption('86400000')
  await page.getByRole('button',{name:'Collapse all',exact:true}).click()
  await expect(page.locator('.ot-job')).toHaveCount(0)
  m=await metrics()
  if (m.scrollHeight>m.height+1) throw Error('Collapse introduced vertical scrolling')
  await page.getByLabel('Timeline row density',{exact:true}).selectOption('compact')
  await page.setViewportSize({width:1100,height:900})
  await expect.poll(async()=> Math.abs((await metrics()).track/((await metrics()).width-(await metrics()).label)-7)).toBeLessThan(.03)
  await page.locator('.ot-scrollbar').focus(); await page.keyboard.press('End')
  await expect(page.getByRole('slider',{name:'Horizontal timeline scrollbar',exact:true})).toBeVisible()
  await page.locator('.ot-scrollbar').hover()
})
