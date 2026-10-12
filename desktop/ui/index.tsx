import { render } from 'solid-js/web';
import App from './App';
import 'maplibre-gl/dist/maplibre-gl.css';
import './style.css';

render(() => <App />, document.getElementById('root')!);
