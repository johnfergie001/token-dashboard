import { api, fmt } from '/web/app.js';
import { barChart } from '/web/charts.js';

export default async function (root) {
  const rows = await api('/api/projects');
  const totalCost = rows.reduce((s, r) => s + (r.cost_usd || 0), 0);
  const isSplit = rows.some(r => r.subproject);

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Projects</h2>
      <span class="spacer"></span>
      <span class="pill" title="Total across all rows below">${fmt.usd(totalCost)} total</span>
    </div>

    ${isSplit ? `
      <p class="muted" style="margin:0 0 14px;font-size:12px">
        Split below the Claude Code session slug using <code>~/.claude/token-dashboard-subprojects.json</code> —
        each session's cost is weighted by the share of its <code>Bash</code>/<code>Read</code>/<code>Edit</code>/<code>Write</code>
        calls matching each project's configured paths. See <code>subprojects.example.json</code> in the repo to set this up.
      </p>` : `
      <p class="muted" style="margin:0 0 14px;font-size:12px">
        Grouped by Claude Code session slug — the folder each session launched in, not necessarily every file it touched.
        Add <code>~/.claude/token-dashboard-subprojects.json</code> (see <code>subprojects.example.json</code>) to split
        cost below the slug when you work across projects (or SSH into remote services) from one launch directory.
      </p>`}

    <div class="card">
      <h3>Cost by project</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Top ${Math.min(10, rows.length)} by est. cost.</p>
      <div id="ch-cost" style="height:320px"></div>
    </div>

    <div class="card" style="margin-top:16px">
      <table>
        <thead><tr>
          <th>project</th><th class="num">cost</th><th class="num">sessions</th>
          <th class="num">turns</th><th class="num">billable tokens</th><th class="num">cache reads</th>
        </tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
              <td class="num" style="color:var(--good)">${fmt.usd(r.cost_usd)}</td>
              <td class="num">${fmt.int(r.sessions)}</td>
              <td class="num">${fmt.int(r.turns)}</td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
            </tr>`).join('') || '<tr><td colspan="6" class="muted">no sessions in this range</td></tr>'}
        </tbody>
      </table>
    </div>`;

  const top = rows.slice().sort((a, b) => b.cost_usd - a.cost_usd).slice(0, 10);
  barChart(document.getElementById('ch-cost'), {
    categories: top.map(r => {
      const name = r.project_name || r.project_slug;
      return name.length > 18 ? name.slice(0, 17) + '…' : name;
    }),
    values: top.map(r => Number((r.cost_usd || 0).toFixed(2))),
    color: '#3FB68B',
  });
}
