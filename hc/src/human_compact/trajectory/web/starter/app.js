// Implement the requested TODO here. The starter is infrastructure, not a completed research task.
// Reuse loadDataset(), selectControl(), renderTable(), and renderTimeline() from ./ui.js.
// loadDataset returns a bounded PAGE of actual active data, never a fabricated dataset.
// Check hasMore/nextOffset before treating a page as the complete dataset.
import { loadDataset, selectControl, renderTable, renderTimeline } from './ui.js';
const app = document.querySelector('#app');
