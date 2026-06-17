const output = document.querySelector('#output');
const healthButton = document.querySelector('#checkHealth');
const configButton = document.querySelector('#checkConfig');

async function callApi(path) {
  output.textContent = `Anropar ${path} ...`;

  try {
    const response = await fetch(path);
    const data = await response.json();
    output.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    output.textContent = `Fel vid API-anrop: ${error.message}`;
  }
}

healthButton.addEventListener('click', () => callApi('/api/health'));
configButton.addEventListener('click', () => callApi('/api/config'));
