// SweetAlert2 modal for task deletion
// Place this in your static/js/gtm-tasklist.js and include it in your playbook template

document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.delete-btn').forEach(btn => {
    btn.addEventListener('click', function(e) {
      e.preventDefault();
      Swal.fire({
        title: 'Delete this task?',
        text: 'This action cannot be undone.',
        icon: 'warning',
        showCancelButton: true,
        confirmButtonColor: '#d33',
        cancelButtonColor: '#3085d6',
        confirmButtonText: 'Yes, delete it!'
      }).then((result) => {
        if (result.isConfirmed) {
          window.location.href = btn.getAttribute('data-url');
        }
      });
    });
  });
});
