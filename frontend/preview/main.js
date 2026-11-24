import policies from '../../data/policies.json'
import { answerPolicy } from './policy.js'
import '../src/style.css'
import './preview.css'

const form = document.querySelector('#question-form')
const input = document.querySelector('#question')
const status = document.querySelector('#answer-status')
const sources = document.querySelector('#answer-sources')
const narrowScreen = window.matchMedia('(max-width: 720px)')

const reply = {
  matched: '找到以下适用条款。',
  missing: '演示知识库未找到足以回答这个问题的资料。可以换个问法，或试试页面中的政策示例。',
  conflict: '检索到相互冲突的政策版本，需要核实后才能确认。',
  live: '订单状态需要在本地完整版查询。此页面可以查询一般售后政策。',
}

function sourceItem({ id, title, text, channel, version, effective_from }) {
  const item = document.createElement('li')
  const heading = document.createElement('h3')
  heading.textContent = title
  const excerpt = document.createElement('p')
  excerpt.textContent = text
  const details = document.createElement('p')
  details.className = 'preview-source-meta'
  details.textContent = `${id} · ${channel} · 版本 ${version} · 生效 ${effective_from}`
  item.append(heading, excerpt, details)
  return item
}

form.addEventListener('submit', (event) => {
  event.preventDefault()
  if (!input.value.trim()) {
    input.setCustomValidity('请输入一个政策问题。')
    input.reportValidity()
    return
  }
  const result = answerPolicy(input.value, policies)
  status.dataset.state = result.status
  status.textContent = reply[result.status]
  sources.replaceChildren(...result.sources.map(sourceItem))
  if (narrowScreen.matches) document.querySelector('#answer-title').scrollIntoView({ block: 'start' })
})

input.addEventListener('input', () => input.setCustomValidity(''))
for (const example of document.querySelectorAll('[data-example]')) {
  example.addEventListener('click', () => {
    input.value = example.textContent.trim()
    input.setCustomValidity('')
    form.requestSubmit()
  })
}
