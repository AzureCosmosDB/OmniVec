window.msalRedirectBridge.broadcastResponseToMainFrame().catch(error => {
  console.error('Microsoft sign-in callback failed.', error);
  document.body.textContent = 'Microsoft sign-in could not be completed. Close this window and try again.';
});
