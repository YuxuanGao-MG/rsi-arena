// How the run reader is deployed.
//
// One Python process serving one HTML file. No build step and no framework: the
// page does three selects against Supabase and renders three tables, which is
// not a reason to take on a toolchain.
//
// SUPABASE_URL and SUPABASE_ANON_KEY are set on the service, not committed. The
// anon key is public by design — a browser would hold it anyway — but injecting
// it at serve time means rotating it does not mean editing HTML.

export default {
  build: { builder: "NIXPACKS" },
  deploy: {
    startCommand: "python web/server.py",
    healthcheckPath: "/health",
    restartPolicyType: "ON_FAILURE",
  },
};
