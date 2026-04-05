import { Controller } from "@hotwired/stimulus"
import Sortable from "sortablejs"

export default class extends Controller {
  connect() {
    this.todoList = this.element.querySelector("#todo-list")
    this.doingList = this.element.querySelector("#doing-list")
    this.doneList = this.element.querySelector("#done-list")

    this.initSortable(this.todoList)
    this.initSortable(this.doingList)
    this.initSortable(this.doneList)
  }

  initSortable(element) {
    new Sortable(element, {
      group: "kanban",
      animation: 150,
      onEnd: this.updateStatus.bind(this),
    })
  }

  updateStatus(event) {
    const itemEl = event.item
    const newStatus = event.to.dataset.status
    const itemId = itemEl.dataset.id
    const csrfToken = document.querySelector("[name=csrfmiddlewaretoken]").value

    if (itemId && newStatus) {
      const url = `/dashboard/action-item/${itemId}/move/${newStatus}/`
      const options = {
        method: "POST",
        headers: {
          "X-CSRFToken": csrfToken,
          "Content-Type": "application/json",
          "Accept": "application/json",
        },
      }

      fetch(url, options)
        .then((response) => {
          if (!response.ok) {
            console.error("Failed to update status")
          }
        })
        .catch((error) => {
          console.error("Error updating status:", error)
        })
    }
  }
}
