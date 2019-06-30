package com.javaproject.demo.controller;

import com.javaproject.demo.mapper.UserMapper;
import com.javaproject.demo.model.User;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.bind.annotation.RequestParam;

import javax.servlet.http.HttpServletRequest;
import java.util.UUID;


@Controller
public class RegisterController {
    @Autowired
    UserMapper userMapper;
    @RequestMapping(value = "/Register",method = {RequestMethod.POST,RequestMethod.GET})
    public String register(@RequestParam(value = "accountid", required = false) String accountid,
                           @RequestParam(value = "password", required = false) String password,
                           @RequestParam(value = "name", required = false) String name,
                           HttpServletRequest request,
                           Model model) {
        model.addAttribute("accountid", accountid);
        model.addAttribute("password", password);
        model.addAttribute("name", name);
        if (accountid == null || accountid == "") {
            model.addAttribute("error", "账号不能为空");
            return "Register";
        }
        if (password == null || password == "") {
            model.addAttribute("error", "密码不能为空");
            return "Register";
        }
        User user=new User();
        String token= UUID.randomUUID().toString();
        user.setToken(token);
        user.setName(name);
        user.setPassword(password);
        user.setAccountId(accountid);
        user.setGmtCreate(System.currentTimeMillis());
        userMapper.insert(user);
        return "redirect:/";
    }
}
