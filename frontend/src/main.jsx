import React from 'react'
import { createRoot } from 'react-dom/client'
import '@xyflow/react/dist/style.css'
import '@fontsource-variable/plus-jakarta-sans/wght.css'
import '@fontsource/rubik/400.css'
import '@fontsource/rubik/500.css'
import App from './App.jsx'
import './styles.css'
import './astel-preview.css'
import './corona-preview.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
