import React from "react";
import ReactDOM from "react-dom/client";

// Fonts are bundled, not fetched from a CDN: this tool has to work on an
// air-gapped verification machine.
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";
import "@fontsource/ibm-plex-sans-condensed/400.css";
import "@fontsource/ibm-plex-sans-condensed/600.css";

import "./styles/tokens.css";
import "./styles/app.css";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
