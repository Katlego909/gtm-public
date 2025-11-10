# 🎨 Chat Avatar Icon Options

## Current Design
✅ **Lightbulb Icon (Ideas/Intelligence)**
- Gradient: Indigo-500 to Purple-600
- Shape: Rounded square (rounded-xl)
- Shadow: Medium shadow-md
- Best for: Professional, modern, "smart assistant" feel

---

## Alternative SVG Icons You Can Use

### Option 1: Brain/Neural Network (Most AI-like)
```html
<svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
</svg>
```

### Option 2: Sparkles/Magic (Creative AI)
```html
<svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
</svg>
```

### Option 3: Chat Bubble (Conversational)
```html
<svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
</svg>
```

### Option 4: Lightning Bolt (Fast/Powerful)
```html
<svg class="w-6 h-6 text-white" fill="currentColor" viewBox="0 0 20 20">
  <path fill-rule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z" clip-rule="evenodd" />
</svg>
```

### Option 5: Chip/Processor (Technical AI)
```html
<svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
</svg>
```

### Option 6: Stars/Award (Premium AI)
```html
<svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />
</svg>
```

### Option 7: Beaker/Lab (Experimental AI)
```html
<svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
</svg>
```

---

## Color Gradient Options

### Current (Professional Purple-Blue)
```html
bg-gradient-to-br from-indigo-500 to-purple-600
```

### Alternative 1: Vibrant Blue-Cyan
```html
bg-gradient-to-br from-blue-500 to-cyan-500
```

### Alternative 2: Green-Teal (Nature/Growth)
```html
bg-gradient-to-br from-green-500 to-teal-600
```

### Alternative 3: Orange-Pink (Energetic)
```html
bg-gradient-to-br from-orange-500 to-pink-600
```

### Alternative 4: Purple-Pink (Creative)
```html
bg-gradient-to-br from-purple-500 to-pink-500
```

### Alternative 5: Dark Professional (Gray-Black)
```html
bg-gradient-to-br from-gray-700 to-gray-900
```

---

## How to Change Icons

### Step 1: Choose Your Icon
Pick from the options above or browse [Heroicons](https://heroicons.com)

### Step 2: Replace in Template
Edit `gtm/templates/gtm/chat.html` and find all instances of:
```html
<svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
</svg>
```

Replace the entire `<svg>` tag with your chosen icon.

### Step 3: Update JavaScript
Find the `addMessage()` function and replace the SVG there too (around line 270).

---

## Pro Tips

1. **Consistency**: Use the same icon throughout (header, messages, loading)
2. **Contrast**: Keep white icons on dark gradients for readability
3. **Size**: 
   - Header/Messages: `w-6 h-6`
   - Buttons: `w-5 h-5`
   - User avatar: `w-5 h-5`
4. **Animation**: Add `animate-pulse` to loading state for extra polish
5. **Brand Alignment**: Match gradient colors to your company brand

---

## Custom Logo Option

Want to use your actual logo? Replace SVG with:

```html
<img src="/static/images/ai-logo.png" alt="AI" class="w-6 h-6">
```

Then add your logo to: `gtm/static/images/ai-logo.png`

---

**Current Setup Uses:** Lightbulb icon with indigo-to-purple gradient ✨
