import { handleRequest } from "./security.js";

// Only a default export. Named exports on the Worker entry are treated as
// additional handlers and fail the runtime if they are not functions.
export default {
  fetch: handleRequest,
};
