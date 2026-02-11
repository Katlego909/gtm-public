import { Controller } from "@hotwired/stimulus"

export default class extends Controller {
  static targets = [ "content" ]

  connect() {
    this.element.addEventListener("turbo:before-stream-render", (event) => {
      if (this.element.contains(event.target)) {
        return
      }

      event.preventDefault()
      this.contentTarget.innerHTML = event.newStream.innerHTML
    })
  }

  open() {
    this.element.classList.remove("hidden")
  }

  close() {
    this.element.classList.add("hidden")
    this.contentTarget.innerHTML = ""
  }
}
