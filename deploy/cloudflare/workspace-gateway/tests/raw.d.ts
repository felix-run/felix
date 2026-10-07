// Vite's `?raw` import: a file's text, as a string (tests only).
declare module '*?raw' {
  const text: string;
  export default text;
}
