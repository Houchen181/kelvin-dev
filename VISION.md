# Lab Harness — Vision (user's original prompt, verbatim)

Saved 2026-10-17. Authoritative spec for this project; re-read before any design or implementation work.

---

I want to build an LLM harness to specifically serve the experimental physicist to assist their work

similar to the browser version of chatgpt and claude, have access to the PC

overall goal:
convenient portal. 
just like a researcher, also advisor. has big picture to plan, at the same time know the details to perform. 


what are the key features:

1. integrated instruments so that the response is faster than average harness
2. can keep project confidential
3. proceed with much caution, to prevent accidental damage to the instrument and device; how to make people trust it? simulated experiment
4. support customized instrument.
5. when performing experiment, do some literature research to propose a experiment plan; 
6. understand each instrument, how to use each instrument under correct condition and test all the instrument and make sure the instrument is working before using them 
7. keep track all the intermediate data, all the experimental conditions. 
8. automatically generate the experimental summary or experimental log, in the form of pdf or ppt, to reflect, review and discuss with other people/advisor
9. only provide concise and accurate response. right now the LLM generates too much to review, too difficult to understand sometimes. too hard to follow
10. mobile access, remote control
11. better literature review;
12. better virtual reviewer?
13. monitor the sensitive instrument and report timely for any error
14. better memory structure, keep the important steps/ concepts into memory.  do not remember the random questions.  global memory/per project memory/per conversation memory, do not leak to each other
15. notification,
16. have the function to enable parallel conversation with multiple models like gpt/claude/gemeni
