/* Shared report controls and accessible SVG charts. Values come from full API aggregates. */
function analyticsDate(value){return new Date(value+'T00:00:00Z').toLocaleDateString('en-US',{month:'short',day:'numeric',timeZone:'UTC'});}
function analyticsRange(prefix,start,end){
  return `<details class="analytics-range" id="${prefix}-range"><summary id="${prefix}-range-toggle">${esc(analyticsDate(start))} – ${esc(analyticsDate(end))}<span aria-hidden="true">⌄</span></summary><div class="analytics-range-menu"><label>Period<select id="${prefix}-period"><option value="custom">Custom dates</option><option value="month">Month to date</option><option value="last-month">Last month</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option></select></label><form id="${prefix}-filter-form" class="analytics-date-form"><label>From<input name="start" id="${prefix}-start" type="date" value="${esc(start)}" required></label><label>Through<input name="end" id="${prefix}-end" type="date" value="${esc(end)}" required></label><button type="submit" class="primary">Apply dates</button><small>Dates in UTC · up to 93 days</small></form></div></details>`;
}
function analyticsPreset(value,today=new Date()){
  const end=new Date(Date.UTC(today.getUTCFullYear(),today.getUTCMonth(),today.getUTCDate())),start=new Date(end);
  if(value==='month')start.setUTCDate(1);
  else if(value==='last-month'){end.setUTCDate(0);start.setUTCMonth(end.getUTCMonth(),1);start.setUTCFullYear(end.getUTCFullYear());}
  else if(['7','30'].includes(value))start.setUTCDate(start.getUTCDate()-Number(value)+1);
  else return null;
  return {start:start.toISOString().slice(0,10),end:end.toISOString().slice(0,10)};
}
function bindAnalyticsPreset(prefix,apply){
  const select=$('#'+prefix+'-period');
  if(select)select.onchange=()=>{const range=analyticsPreset(select.value);if(range){$('#'+prefix+'-range').open=false;$('#'+prefix+'-range-toggle')?.focus();apply(range);}};
}
function analyticsMetrics(items){return `<div class="analytics-metrics">${items.map(([label,value,note])=>`<div><span>${esc(label)}</span><strong>${esc(value)}</strong>${note?`<small>${esc(note)}</small>`:''}</div>`).join('')}</div>`;}
function analyticsLegend(series,cumulative=false){return `<div class="analytics-legend">${series.map((s,i)=>`<span><i class="${s.line?'analytics-average-key':'analytics-color-'+i%5}"></i>${esc(s.label)}</span>`).join('')}${cumulative?'<span><i class="analytics-line-key"></i>Cumulative</span>':''}</div>`;}
function analyticsChart(rows,series,{title,money=false,area=false,cumulative=false,line=null}={}){
  const width=960,height=260,left=money?66:44,right=cumulative?70:18,top=18,bottom=40;
  const plotWidth=width-left-right,plotHeight=height-top-bottom,step=plotWidth/Math.max(1,rows.length);
  const value=(row,key)=>Math.max(0,Number(row[key])||0),totals=rows.map(row=>series.reduce((sum,s)=>sum+value(row,s.key),0));
  const max=Math.max(0,...totals,...(line?rows.map(r=>value(r,line.key)):[]))||1,scale=10**Math.floor(Math.log10(max)),ceiling=money?Math.ceil(max/scale)*scale:Math.ceil(max/4/scale)*scale*4;
  const total=totals.reduce((a,b)=>a+b,0),y=v=>top+plotHeight*(1-v/ceiling),x=i=>left+step*(i+.5);
  const format=v=>money?'$'+new Intl.NumberFormat('en-US',{maximumFractionDigits:v<1?6:0}).format(v):new Intl.NumberFormat('en-US',{maximumFractionDigits:1}).format(v);
  const grid=Array.from({length:5},(_,i)=>{const v=ceiling*i/4;return `<line x1="${left}" x2="${width-right}" y1="${y(v)}" y2="${y(v)}" class="analytics-grid"/><text x="${left-10}" y="${y(v)+4}" text-anchor="end">${format(v)}</text>${cumulative?`<text x="${width-right+10}" y="${y(v)+4}">${format(total*i/4)}</text>`:''}`;}).join('');
  let marks='';
  if(area){
    const points=rows.map((row,i)=>`${x(i)},${y(value(row,series[0].key))}`).join(' ');
    if(rows.length)marks=`<polygon points="${x(0)},${y(0)} ${points} ${x(rows.length-1)},${y(0)}" class="analytics-area"/><polyline points="${points}" class="analytics-trend"/>`;
    marks+=rows.map((row,i)=>`<circle cx="${x(i)}" cy="${y(value(row,series[0].key))}" r="3" class="analytics-point"><title>${esc(row.date)}: ${format(value(row,series[0].key))}</title></circle>`).join('');
  }else{
    marks=rows.map((row,i)=>{let sum=0;return series.map((s,j)=>{const v=value(row,s.key);sum+=v;return `<rect x="${x(i)-step*.34}" y="${y(sum)}" width="${step*.68}" height="${plotHeight*v/ceiling}" class="analytics-color-${j%5}${row.partial?' analytics-partial':''}"><title>${esc(row.date)} · ${esc(s.label)}: ${money?dollars(v):spendCount(v)}</title></rect>`;}).join('');}).join('');
  }
  if(line){const points=rows.map((row,i)=>`${x(i)},${y(value(row,line.key))}`).join(' ');marks+=`<polyline points="${points}" class="analytics-average"/>`;}
  if(cumulative){let sum=0;const points=totals.map((v,i)=>{sum+=v;return `${x(i)},${top+plotHeight*(1-sum/(total||1))}`;}).join(' ');marks+=`<polyline points="${points}" class="analytics-cumulative"/>`;if(rows.length===1)marks+=`<circle cx="${x(0)}" cy="${top+plotHeight*(1-total/(total||1))}" r="3" class="analytics-cumulative-point"/>`;}
  const ticks=rows.map((row,i)=>i===0||i===rows.length-1||(i%Math.max(1,Math.ceil(rows.length/9))===0&&rows.length-1-i>Math.ceil(rows.length/9)*.6)?`<text x="${x(i)}" y="${height-12}" text-anchor="middle">${esc(analyticsDate(row.date))}</text>`:'').join('');
  return `<div class="analytics-chart" role="region" aria-label="${esc(title)}" tabindex="0"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}. Exact values in the breakdown below."><title>${esc(title)}</title>${grid}${marks}${ticks}</svg></div>`;
}
function analyticsCSV(rows){
  return rows.map(row=>row.map(value=>{let text=String(value??'');if(/^[\s]*[=+@-]/.test(text))text="'"+text;return '"'+text.replaceAll('"','""')+'"';}).join(',')).join('\r\n');
}
function downloadAnalyticsCSV(filename,rows){
  const url=URL.createObjectURL(new Blob(['\ufeff'+analyticsCSV(rows)],{type:'text/csv;charset=utf-8;'})),link=document.createElement('a');
  link.href=url;link.download=filename;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
