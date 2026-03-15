const state = {
  token: null,
  user: null,
  evt: null,
};

const authState = document.getElementById('authState');
const submitState = document.getElementById('submitState');

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (!headers['Content-Type'] && options.body) headers['Content-Type'] = 'application/json';

  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || 'Erro na API');
  }
  return payload;
}

function setVisible(id, visible) {
  const el = document.getElementById(id);
  el.classList.toggle('hidden', !visible);
}

function updateDashboard(data) {
  document.getElementById('totalRecords').textContent = data.summary.total_records;
  document.getElementById('avgValue').textContent = data.summary.average_value;
  document.getElementById('generatedAt').textContent = new Date(data.generated_at).toLocaleString();

  const bucketList = document.getElementById('bucketList');
  bucketList.innerHTML = '';
  Object.entries(data.bucket_distribution).forEach(([bucket, total]) => {
    const li = document.createElement('li');
    li.textContent = `${bucket}: ${total}`;
    bucketList.appendChild(li);
  });

  const regionList = document.getElementById('regionList');
  regionList.innerHTML = '';
  data.regions.forEach((r) => {
    const li = document.createElement('li');
    li.textContent = `${r.region}: ${r.total} registos (média ${Number(r.avg_value || 0).toFixed(2)})`;
    regionList.appendChild(li);
  });

  const body = document.getElementById('recentBody');
  body.innerHTML = '';
  data.recent_records.forEach((rec) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${rec.source}</td>
      <td>${rec.region}</td>
      <td>${rec.area}</td>
      <td>${Number(rec.value).toFixed(2)}</td>
      <td>${rec.bucket}</td>
      <td>${new Date(rec.received_at).toLocaleTimeString()}</td>
    `;
    body.appendChild(tr);
  });
}

async function refreshDashboard() {
  const region = document.getElementById('regionFilter').value.trim();
  const query = region ? `?region=${encodeURIComponent(region)}` : '';
  const data = await api(`/api/dashboard${query}`);
  updateDashboard(data);
}

function connectRealtime() {
  if (state.evt) state.evt.close();
  state.evt = new EventSource(`/api/events`, {
    withCredentials: false,
  });

  state.evt.onmessage = () => {};
  state.evt.addEventListener('update', (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === 'dashboard_update') {
      updateDashboard(payload.data);
    }
  });
  state.evt.onerror = () => {
    authState.textContent = 'Ligação em tempo real instável. A tentar reconectar...';
  };
}

document.getElementById('loginBtn').addEventListener('click', async () => {
  try {
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value.trim();
    const result = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    state.token = result.token;
    state.user = result.user;
    authState.textContent = `Autenticado como ${result.user.username} (${result.user.role})`;

    setVisible('dashboardCard', true);
    setVisible('submitCard', ['operator', 'supervisor', 'admin'].includes(result.user.role));
    setVisible('historyCard', ['supervisor', 'admin'].includes(result.user.role));

    await refreshDashboard();
    connectRealtime();
  } catch (err) {
    authState.textContent = err.message;
  }
});

document.getElementById('sendDataBtn').addEventListener('click', async () => {
  try {
    const payload = {
      source: document.getElementById('source').value,
      region: document.getElementById('region').value,
      area: document.getElementById('area').value,
      value: Number(document.getElementById('value').value),
    };
    const result = await api('/api/data', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    submitState.textContent = `Dado processado com sucesso (bucket: ${result.bucket})`;
    await refreshDashboard();
  } catch (err) {
    submitState.textContent = err.message;
  }
});

document.getElementById('refreshBtn').addEventListener('click', refreshDashboard);

document.getElementById('loadHistoryBtn').addEventListener('click', async () => {
  try {
    const result = await api('/api/history');
    const list = document.getElementById('historyList');
    list.innerHTML = '';
    result.entries.forEach((entry) => {
      const li = document.createElement('li');
      li.textContent = `[${new Date(entry.created_at).toLocaleString()}] ${entry.actor || 'Sistema'} -> ${entry.action} | ${entry.details}`;
      list.appendChild(li);
    });
  } catch (err) {
    document.getElementById('historyList').innerHTML = `<li>${err.message}</li>`;
  }
});
