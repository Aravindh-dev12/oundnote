module.exports = {
  version: "8.0.0",
  title: "Oundnote",
  description:
    "Private, local-first AI meeting assistant for recording, transcription, speaker diarization, searchable meeting history, and structured notes.",
  icon: "src/local_meeting_ai/web/static/icons/mark.svg",
  menu: async (_kernel, info) => {
    if (info.running("install.json") || info.running("update.json")) {
      return [
        {
          icon: "fa-solid fa-spinner",
          text: "Preparing Oundnote...",
          href: info.running("install.json") ? "install.json" : "update.json",
          default: true,
        },
      ]
    }

    if (!info.exists(".pinokio-installed")) {
      return [
        {
          icon: "fa-solid fa-download",
          text: "Install Oundnote",
          href: "install.json",
          default: true,
        },
      ]
    }

    if (info.running("start.json")) {
      const memory = info.local("start.json")
      const menu = [
        {
          icon: "fa-solid fa-terminal",
          text: "Server log",
          href: "start.json",
        },
      ]
      if (memory && memory.url) {
        menu.unshift({
          icon: "fa-solid fa-arrow-up-right-from-square",
          text: "Open Oundnote",
          href: memory.url,
          default: true,
        })
      }
      return menu
    }

    return [
      {
        icon: "fa-solid fa-play",
        text: "Start Oundnote",
        href: "start.json",
        default: true,
      },
      {
        icon: "fa-solid fa-rotate",
        text: "Update",
        href: "update.json",
      },
      {
        icon: "fa-solid fa-screwdriver-wrench",
        text: "Repair installation",
        href: "reset.json",
      },
    ]
  },
}
