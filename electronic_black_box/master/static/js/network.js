const container = document.getElementById('network');
let network;

// feste Positionen: 1..6 außen (1 links, Uhrzeigersinn), 7 Mitte
const R = 280;
const POS = {};
for (let i = 1; i <= 6; i++) {
  const angle = Math.PI - (2 * Math.PI * (i - 1)) / 6;
  POS[i] = { x: Math.cos(angle) * R, y: Math.sin(angle) * R };
}
POS[7] = { x: 0, y: 0 };

function rssiColor(rssi){
  if (rssi == null || isNaN(rssi)) return '#9ca3af';
  const v = Number(rssi);
  if (v > -70) return 'green';
  if (v > -85) return 'orange';
  return 'red';
}

function buildNodes(presence, lastRssiByNode, activeId) {
  const ids = ['1','2','3','4','5','6','7'];
  return ids.map(id => {
    const online = (presence[id]?.status === 'online');

    // Standard-Optik
    let bg  = online ? '#E8F7EE' : '#EEEEEE';
    let brd = '#9AA3AF';
    let bw  = 1;
    let size = 12;

    // Aktuell gelesener Knoten: klarer blauer Fokus
    if (id === activeId) {
      bg   = '#DBEAFE';          // blaues Hintergrund-Highlight
      brd  = '#1D4ED8';          // kräftig blau
      bw   = 5;
      size = 18;
    }

    // RSSI in die Node-Label (nur für 1..6, aus 7→id)
    const rssiTxt = (id !== '7' && lastRssiByNode[id] != null)
      ? `\n${Math.round(lastRssiByNode[id])} dBm` : '';

    return {
      id,
      label: `ID ${id}${rssiTxt}`,
      x: POS[+id].x, y: POS[+id].y, fixed: {x:true,y:true},
      shape: 'dot',
      color: { background: bg, border: brd },
      borderWidth: bw,
      font: { size: 13, color: '#111827', multi: true },
      size
    };
  });
}

async function loadConnections() {
  const res = await axios.get('/data', {timeout: 2000});
  const data = res.data.connections || {};
  const presence = res.data.presence || {};
  const activeId = res.data.active ? String(res.data.active) : ''; // ← vom Backend gesetzt

  render(data, presence, activeId);
}

function render(data, presence, activeId) {
  // letzte RSSI je Node (nur von 7→i)
  const lastRssiByNode = {};
  if (data['7']) {
    for (const r in data['7']) {
      const v = data['7'][r];
      if (v && typeof v.rssi !== 'undefined') lastRssiByNode[r] = v.rssi;
    }
  }

  // Edges: stabil nach RSSI einfärben; NUR die aktuell gelesene Kante 7→activeId wird blau & dick
  const edges = [];
  for (const s in data) {
    for (const r in data[s]) {
      const entry = data[s][r] || {};
      const rssi = entry.rssi;
      const isActiveEdge = (s === '7' && r === activeId);

      edges.push({
        id: `${s}>${r}`,
        from: s, to: r, arrows: 'to',
        label: `${Math.round(rssi)} dBm`,
        title: `RSSI: ${Math.round(rssi)} dBm`,
        color: { color: isActiveEdge ? '#1D4ED8' : rssiColor(rssi) }, // nur hier blau
        width: isActiveEdge ? 5 : 2,
        smooth: { type: (+s < +r) ? 'curvedCW' : 'curvedCCW', roundness: 0.25 },
        font: { align: 'middle', size: 12, bold: isActiveEdge }
      });
    }
  }

  const nodes = buildNodes(presence, lastRssiByNode, activeId);
  const visData = { nodes, edges };
  const options = {
    physics: false,
    interaction: { hover: true },
    nodes: { shape: 'dot' },
    edges: { smooth: true, arrows: { to: { enabled: true } } }
  };

  if (!network) {
    network = new vis.Network(container, visData, options);
  } else {
    network.setData(visData);
    network.setOptions(options);
  }
}

setInterval(loadConnections, 900);
loadConnections();
